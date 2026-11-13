use std::ffi::c_ulong;
use std::process::ExitCode;

#[link(name = "crypto", kind = "static")]
extern "C" {
    fn OpenSSL_version_num() -> c_ulong;
}

fn main() -> ExitCode {
    // This OpenSSL API takes no pointers and returns the linked library's version.
    let version = unsafe { OpenSSL_version_num() };
    println!("OpenSSL version: {version:#x}");
    if version >= 0x3000_0000 {
        ExitCode::SUCCESS
    } else {
        ExitCode::FAILURE
    }
}
