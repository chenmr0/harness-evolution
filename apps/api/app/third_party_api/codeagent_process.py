"""Windows process-tree cleanup adapted from the local Skill evolution demo."""

import sys
from typing import Any

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenThread.restype = wintypes.HANDLE
    kernel.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel.ResumeThread.restype = wintypes.DWORD

    class ThreadEntry(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD),
            ("usage", wintypes.DWORD),
            ("thread_id", wintypes.DWORD),
            ("process_id", wintypes.DWORD),
            ("base_priority", wintypes.LONG),
            ("delta_priority", wintypes.LONG),
            ("flags", wintypes.DWORD),
        ]

    kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
    kernel.Thread32First.restype = wintypes.BOOL
    kernel.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
    kernel.Thread32Next.restype = wintypes.BOOL

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("ProcessTime", ctypes.c_int64),
            ("JobTime", ctypes.c_int64),
            ("Flags", wintypes.DWORD),
            ("MinWorkingSet", ctypes.c_size_t),
            ("MaxWorkingSet", ctypes.c_size_t),
            ("ActiveProcesses", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("Priority", wintypes.DWORD),
            ("Scheduling", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_uint64)
            for name in ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("Basic", BasicLimits),
            ("Io", IoCounters),
            ("ProcessMemory", ctypes.c_size_t),
            ("JobMemory", ctypes.c_size_t),
            ("PeakProcessMemory", ctypes.c_size_t),
            ("PeakJobMemory", ctypes.c_size_t),
        ]

    class WindowsJob:
        """Kill assigned processes/descendants on close; not a security sandbox."""

        def __init__(self) -> None:
            self.handle: Any = None
            if sys.platform == "win32":
                self.handle = kernel.CreateJobObjectW(None, None)
                if not self.handle:
                    raise OSError("Cannot create CLI process job")
                limits = ExtendedLimits()
                limits.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                if not kernel.SetInformationJobObject(
                    self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
                ):
                    self.close()
                    raise OSError("Cannot configure CLI process job")

        def attach(self, pid: int) -> None:
            if self.handle:
                process = kernel.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE
                if not process:
                    raise OSError("Cannot open CLI process")
                try:
                    if not kernel.AssignProcessToJobObject(self.handle, process):
                        raise OSError("Cannot attach CLI process to job")
                finally:
                    kernel.CloseHandle(process)

        def close(self) -> None:
            if self.handle:
                kernel.CloseHandle(self.handle)
                self.handle = None

        @staticmethod
        def resume(pid: int) -> None:
            """Resume the suspended primary thread only after job assignment.

            This closes the launch race where a Windows Python/npm launcher could
            create children before the parent was attached to the job.
            """
            snapshot = kernel.CreateToolhelp32Snapshot(0x4, 0)  # TH32CS_SNAPTHREAD
            if snapshot == ctypes.c_void_p(-1).value:
                raise OSError("Cannot inspect suspended CLI thread")
            try:
                entry = ThreadEntry()
                entry.size = ctypes.sizeof(entry)
                found = kernel.Thread32First(snapshot, ctypes.byref(entry))
                while found:
                    if entry.process_id == pid:
                        thread = kernel.OpenThread(0x0002, False, entry.thread_id)
                        if not thread:
                            raise OSError("Cannot open suspended CLI thread")
                        try:
                            if kernel.ResumeThread(thread) == 0xFFFFFFFF:
                                raise OSError("Cannot resume CLI thread")
                            return
                        finally:
                            kernel.CloseHandle(thread)
                    found = kernel.Thread32Next(snapshot, ctypes.byref(entry))
                raise OSError("Suspended CLI thread not found")
            finally:
                kernel.CloseHandle(snapshot)

else:

    class WindowsJob:
        """No-op Windows lifecycle on POSIX, which uses process groups instead."""

        def attach(self, pid: int) -> None:
            """POSIX processes are already started in their own session."""

        def close(self) -> None:
            """POSIX cleanup is performed by the caller's killpg boundary."""

        @staticmethod
        def resume(pid: int) -> None:
            """POSIX subprocesses are not created suspended."""
