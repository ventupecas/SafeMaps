"""The native library (backend/rust_kernel), which holds the route search and the geometry a plan repeats.

Built with: cargo build --release --manifest-path backend/rust_kernel/Cargo.toml
ROUTES_KERNEL_LIBRARY says where it is, if not where that puts it. ctypes lets go of Python's lock during every call,
so trips plan side by side on the computer's cores.
"""

from __future__ import annotations

import ctypes as C
import os
import sys
from pathlib import Path

import numpy as np

_NAMES = {"win32": "route_kernel.dll", "darwin": "libroute_kernel.dylib"}
_NAME = _NAMES.get(sys.platform, "libroute_kernel.so")
LIBRARY = Path(os.environ.get("ROUTES_KERNEL_LIBRARY") or Path(__file__).resolve().parent.parent / "rust_kernel" / "target" / "release" / _NAME)

if not LIBRARY.exists():
    raise RuntimeError(f"The route kernel isn't built: no {LIBRARY}. Build it with: cargo build --release --manifest-path backend/rust_kernel/Cargo.toml")

lib = C.CDLL(str(LIBRARY))
lib.checks_arrays.restype = C.c_bool

pointer, i64, i32, f64, boolean, u8 = C.c_void_p, C.c_int64, C.c_int32, C.c_double, C.c_bool, C.c_uint8


class Arr(C.Structure):
    """An array Python owns, as Rust reads it."""

    _fields_ = [("p", pointer), ("n", i64)]


class Out(C.Structure):
    """An array Rust made, for Python to copy and then free."""

    _fields_ = [("p", pointer), ("n", i64)]


def arr(values: np.ndarray) -> Arr:
    return Arr(values.ctypes.data, values.size)


def address(values: np.ndarray) -> int:
    return values.ctypes.data


def copied(out: Out, dtype: type) -> np.ndarray:
    """What Rust made, as an array of Python's."""
    if not out.n:
        return np.empty(0, dtype=dtype)
    return np.frombuffer(C.string_at(out.p, out.n * np.dtype(dtype).itemsize), dtype=dtype)


def declare(name: str, argtypes: list, restype: object = None) -> None:
    function = getattr(lib, name)
    function.argtypes = argtypes
    function.restype = restype
