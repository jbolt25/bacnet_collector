# Rust Integration Risk Audit

## Answer these questions

### 1. Can a Rust/PyO3 module be introduced without substantially changing the application's deployment model?
Yes, a Rust/PyO3 module can be integrated with minimal changes to the actual deployment process, provided prebuilt wheels are generated. The current deployment runs on Raspberry Pi OS inside a virtual environment, managed by systemd (`bacnet-console.service`). If the Rust extension is packaged as a standard Python wheel, the production environment simply runs `pip install` as it currently does. However, building *from source* natively on the Raspberry Pi during deployment would be a substantial and risky change (due to memory constraints and compile times), making cross-compiled wheels a necessity. The strict systemd sandboxing (`MemoryDenyWriteExecute=true`, `NoNewPrivileges=true`) is generally compatible with standard compiled Rust binaries/extensions, provided no JIT compilation is introduced.

### 2. How should Rust code live in this repository?
The Rust code should be kept isolated from the Python source tree but managed within the same repository to ensure atomic commits and synchronized versioning.

A recommended structure is placing a `rust/` directory at the project root:
`rust/bacnet_backend/`

This keeps the Rust toolchain (Cargo) and Python toolchain (setuptools/maturin) cleanly separated, avoiding cluttering the `src/bacnet_console` package, while allowing `maturin` to reference the Rust crate via `pyproject.toml`.

### 3. How should Python call Rust?
**Comparison:**
- **PyO3 native extension:** Simplest deployment (single process, same systemd unit), avoids serialization/IPC overhead. High risk of Tokio/asyncio event loop contention if not architected correctly.
- **Subprocess/service boundary:** Highest isolation. A separate Rust daemon managed by systemd or Python. Eliminates GIL and async contention. Adds IPC overhead (Unix domain sockets/JSON/Protobuf) and complicates the deployment model (managing two services or child processes).
- **Local socket/IPC:** Similar to above.
- **HTTP service:** Overkill for a local backend, introduces unnecessary network stack overhead and latency for intra-device communication.

**Recommendation:** **PyO3 native extension (In-process)**.
Despite the async risks, the architectural goal is a "narrow BACnet backend boundary." Managing a second daemon on a constrained Raspberry Pi adds operational complexity that outweighs the benefits of process isolation. A PyO3 extension built with `maturin` allows Python to maintain control over the service lifecycle and configuration.

### 4. How should asynchronous Rust networking interact with the existing Python application?
The application heavily relies on Python's `asyncio`. Attempting to weave Rust's `tokio` runtime directly into the `asyncio` event loop (e.g., running Tokio tasks on the Python thread) is highly error-prone and can lead to GIL deadlocks or blocked event loops.

Instead, the Rust extension should spawn its own isolated `tokio` runtime in a background OS thread upon initialization.
- Python `asyncio` tasks will communicate with the Rust thread using thread-safe asynchronous channels (e.g., passing a command and a Python callback/Future).
- When Rust completes a network request (like a BACnet Who-Is or ReadProperty), it will use `asyncio.get_running_loop().call_soon_threadsafe(...)` to wake up the awaiting Python task.
This entirely decouples the networking I/O from the Python GIL.

### 5. What will local development require?
Local development will require:
- Installing the Rust toolchain via `rustup`.
- Adding `maturin` to the Python virtual environment.
- Modifying the development workflow to run `maturin develop` to compile and link the Rust extension, rather than relying purely on Python's standard library or pure-Python BACpypes.

### 6. What will production deployment require?
For the Raspberry Pi target, production deployment should strictly avoid compiling Rust code on the device itself.
It will require:
- Fetching a pre-compiled ARM32 or ARM64 (aarch64) wheel for the target architecture.
- The `bacnet-console.service` will continue to use its existing virtual environment, but the pip install step will now include the native extension.

### 7. What changes would CI need?
Currently, the project lacks CI/CD configuration. To safely introduce Rust, CI must be established to:
- Run Python tests (`pytest`) against the compiled Rust extension.
- Utilize cross-compilation tools (like `cross` or `maturin-action` with Zig) to automatically build wheels for Linux ARMv7 (Raspberry Pi 32-bit) and AArch64 (Raspberry Pi 64-bit) on GitHub Actions.
- Ensure formatting and linting (`cargo fmt`, `cargo clippy`) pass.

### 8. What are the likely Windows/Linux compatibility issues?
Since the target is a Raspberry Pi OS service, Linux is the primary focus. However, developers likely use Windows or macOS.
- **Networking:** UDP socket behavior (e.g., `SO_REUSEADDR` vs `SO_REUSEPORT`) differs between Windows and Linux. Rust's standard library and Tokio abstract most of this, but platform-specific socket configurations might require `#[cfg(unix)]` / `#[cfg(windows)]` conditionally compiled code.
- **Builds:** Developers on Windows will need Windows wheels built by CI or rely on local MSVC builds, which behave differently than the musl/glibc Linux targets.

### 9. Would prebuilt wheels be practical?
Yes, they are practically **mandatory** for this deployment target. Compiling large Rust applications (especially with Tokio and PyO3) on a Raspberry Pi can easily exhaust memory (OOM) and take an unreasonable amount of time. Prebuilt wheels simplify the deployment to a standard `pip install` from a private index or local directory.

### 10. What are the highest-risk integration points?
- **Event Loop Contention:** Blocking the Python `asyncio` event loop while waiting for Rust Tokio networking, leading to application hangs.
- **Service Shutdown:** Ensuring the background Rust Tokio runtime intercepts Python's `SIGTERM` shutdown sequence, gracefully closing UDP sockets and threads without hanging systemd.
- **Cross-Compilation C-ABI:** Ensuring the generated ARM wheels are compatible with the specific glibc version of the Raspberry Pi OS target.
- **Exception Conversion:** Safely translating Rust networking errors into Python exceptions so the existing `bacnet_console` error handling/logging remains functional.

## Risk register

| Risk | Probability | Impact | Evidence | Mitigation |
|---|---|---|---|---|
| Native build failures on Pi | HIGH | HIGH | Rust compilation (Tokio/PyO3) is resource-intensive and likely to OOM a Raspberry Pi. | Strictly mandate prebuilt cross-compiled wheels for deployment. Do not distribute `sdist` to production. |
| Tokio/asyncio contention | MODERATE | HIGH | Python `asyncio` and Rust `tokio` runtimes can deadlock if they share threads or block each other. | Run Tokio in a dedicated OS thread. Use thread-safe channels and `call_soon_threadsafe` to cross the boundary. |
| Application shutdown hangs | HIGH | MODERATE | Rust background threads running sockets won't automatically close when Python's main loop stops. | Implement a dedicated shutdown hook in the PyO3 module called from Python's shutdown signal handlers. |
| Exception conversion loss | LOW | MODERATE | `ReadResult` in `src/bacnet_console/bacnet.py` expects specific string errors. Rust errors might crash if unhandled. | Implement explicit `impl From<RustError> for PyErr` in PyO3 to map Rust network errors to standard Python exceptions. |
| Platform-specific networking | MODERATE | LOW | Windows/macOS developers might face issues testing UDP broadcast vs Linux Pi target. | Use Rust `socket2` crate for precise socket control and abstract network logic with CI tests on Linux. |
| GIL interactions | LOW | MODERATE | Heavy Rust networking could hold the GIL, blocking Python's dashboard. | Ensure all long-running Rust functions release the GIL (`py.allow_threads`). |

## Recommended repository structure

```
.
├── pyproject.toml                     # Updated to use maturin build backend
├── rust/                              # Root directory for Rust code
│   └── bacnet_backend/
│       ├── Cargo.toml
│       └── src/
│           ├── lib.rs                 # PyO3 module definition
│           ├── runtime.rs             # Tokio background thread manager
│           └── network.rs             # BACnet networking logic
├── src/
│   └── bacnet_console/                # Unmodified existing Python app
│       ├── __init__.py
│       └── ...
└── packaging/
    └── bacnet-console.service         # Existing systemd file (unchanged)
```

## Migration readiness verdict

**MODERATE integration risk**

The architectural boundaries of the existing Python application are well-defined (e.g., `BacpypesScanClient` and `TrendOnlyView`), making a backend swap highly feasible. The deployment model via systemd is robust and compatible with native extensions.

However, the risk is elevated from LOW to MODERATE because:
1. The project currently lacks CI/CD, which will be strictly required to build and test ARM/AArch64 wheels. Relying on local compilation for Raspberry Pi deployment is a non-starter.
2. Integrating two asynchronous runtimes (`asyncio` and `tokio`) within the same process boundary requires careful orchestration to avoid deadlocks, GIL contention, and zombie background threads during systemd restarts.

If cross-compiled CI and proper thread isolation are established early, this migration has a high probability of success.
