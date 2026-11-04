//! The route search and the geometry a plan repeats, as native code.
//!
//! app.search_kernel and app.geo_kernel call these through ctypes, which lets go of Python's lock for each call, so
//! trips plan side by side on the computer's cores. Python owns the arrays, and hands each over with its length.

#![allow(clippy::missing_safety_doc, clippy::too_many_arguments)]

pub mod geometry;
pub mod search;

/// An array Python owns. Reads aren't checked in a normal build, as checking each one costs too much in the search;
/// the `checked` feature checks them all, for running the tests.
#[repr(C)]
pub struct Arr<T> {
    pub p: *mut T,
    pub n: i64,
}

impl<T> Clone for Arr<T> {
    #[inline(always)]
    fn clone(&self) -> Self {
        *self
    }
}
impl<T> Copy for Arr<T> {}

impl<T: Copy> Arr<T> {
    #[inline(always)]
    pub fn new(p: *const T, n: i64) -> Self {
        Arr { p: p as *mut T, n }
    }

    #[inline(always)]
    pub fn len(&self) -> usize {
        self.n as usize
    }

    #[inline(always)]
    pub fn is_empty(&self) -> bool {
        self.n == 0
    }

    #[inline(always)]
    pub unsafe fn get(&self, i: usize) -> T {
        #[cfg(feature = "checked")]
        assert!(i < self.n as usize, "read at {} of an array of {}", i, self.n);
        *self.p.add(i)
    }

    #[inline(always)]
    pub unsafe fn set(&self, i: usize, value: T) {
        #[cfg(feature = "checked")]
        assert!(i < self.n as usize, "write at {} of an array of {}", i, self.n);
        *self.p.add(i) = value;
    }

    /// The part from start up to stop.
    #[inline(always)]
    pub unsafe fn part(&self, start: usize, stop: usize) -> Arr<T> {
        #[cfg(feature = "checked")]
        assert!(start <= stop && stop <= self.n as usize, "part {}..{} of an array of {}", start, stop, self.n);
        Arr { p: self.p.add(start), n: (stop - start) as i64 }
    }
}

/// An array Rust made, for Python to copy and then free: free_i32, free_f64, free_u8, free_i64.
#[repr(C)]
pub struct Out<T> {
    pub p: *mut T,
    pub n: i64,
}

impl<T> Out<T> {
    pub fn from_vec(values: Vec<T>) -> Self {
        let n = values.len() as i64;
        Out { p: Box::into_raw(values.into_boxed_slice()) as *mut T, n }
    }

    pub unsafe fn free(&mut self) {
        if !self.p.is_null() {
            drop(Box::from_raw(std::ptr::slice_from_raw_parts_mut(self.p, self.n as usize)));
        }
        self.p = std::ptr::null_mut();
        self.n = 0;
    }
}

#[no_mangle]
pub unsafe extern "C" fn free_i32(out: *mut Out<i32>) {
    (*out).free();
}

#[no_mangle]
pub unsafe extern "C" fn free_i64(out: *mut Out<i64>) {
    (*out).free();
}

#[no_mangle]
pub unsafe extern "C" fn free_f64(out: *mut Out<f64>) {
    (*out).free();
}

#[no_mangle]
pub unsafe extern "C" fn free_u8(out: *mut Out<u8>) {
    (*out).free();
}

/// Whether this is the build that checks every array access.
#[no_mangle]
pub extern "C" fn checks_arrays() -> bool {
    cfg!(feature = "checked")
}
