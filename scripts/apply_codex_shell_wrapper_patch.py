#!/usr/bin/env python3
"""Patch core/src/shell.rs so derive_exec_args prepends $CODEX_TOOL_PERF_WRAPPER
to the shell argv when that env var is set. Run from the Rust workspace root
(the directory containing core/src/shell.rs), e.g. agent-src/codex-rs/."""
from pathlib import Path

path = Path("core/src/shell.rs")
if not path.exists():
    raise SystemExit("Run from the codex-rs root; core/src/shell.rs not found")

text = path.read_text()

if "CODEX_TOOL_PERF_WRAPPER" in text:
    raise SystemExit("shell.rs already contains CODEX_TOOL_PERF_WRAPPER; nothing to do")

start = text.find("pub fn derive_exec_args(")
if start == -1:
    raise SystemExit(
        "Could not find derive_exec_args. The fork may have moved/renamed the "
        "shell argv constructor; grep for where bash -lc argv is built and "
        "adapt this patcher."
    )
brace_start = text.find("{", start)
if brace_start == -1:
    raise SystemExit("Could not find opening brace")
depth = 0
end = None
for i in range(brace_start, len(text)):
    ch = text[i]
    if ch == "{":
        depth += 1
    elif ch == "}":
        depth -= 1
        if depth == 0:
            end = i + 1
            break
if end is None:
    raise SystemExit("Could not find derive_exec_args end")

new_fn = """pub fn derive_exec_args(&self, command: &str, use_login_shell: bool) -> Vec<String> {
    let args = match self.shell_type {
        ShellType::Zsh | ShellType::Bash | ShellType::Sh => {
            let arg = if use_login_shell { "-lc" } else { "-c" };
            vec![
                self.shell_path.to_string_lossy().to_string(),
                arg.to_string(),
                command.to_string(),
            ]
        }
        ShellType::PowerShell => {
            let mut args = vec![self.shell_path.to_string_lossy().to_string()];
            if !use_login_shell {
                args.push("-NoProfile".to_string());
            }
            args.push("-Command".to_string());
            args.push(command.to_string());
            args
        }
        ShellType::Cmd => {
            let mut args = vec![self.shell_path.to_string_lossy().to_string()];
            args.push("/c".to_string());
            args.push(command.to_string());
            args
        }
    };
    if let Ok(wrapper) = std::env::var("CODEX_TOOL_PERF_WRAPPER") {
        if !wrapper.is_empty() {
            let mut wrapped = Vec::with_capacity(args.len() + 1);
            wrapped.push(wrapper);
            wrapped.extend(args);
            return wrapped;
        }
    }
    args
}"""

backup = path.with_suffix(path.suffix + ".bak.toolperf")
backup.write_text(text)
path.write_text(text[:start] + new_fn + text[end:])
print(f"Patched {path}; backup at {backup}")
