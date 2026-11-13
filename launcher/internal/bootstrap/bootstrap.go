// Package bootstrap prepares a release bundle for its first run (ADR-0003,
// principle 4 "installable without IT knowledge"). Two ways, in order:
//
//  1. The bundle carries its own interpreter with every package already in
//     its site-packages (<bundle>/python, ADR-0051): nothing to prepare, no
//     network, the core runs from the sources in <core>/src.
//  2. Otherwise the Python environment of the core is created with uv - a
//     single static binary shipped next to the launcher - and the
//     interpreter itself is downloaded by uv into the data directory
//     (needs the network once). After that the installation runs offline.
package bootstrap

import (
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
)

// VenvPython returns the interpreter of a synced environment in coreDir,
// or "" when there is none yet.
func VenvPython(coreDir string) string {
	candidates := []string{
		filepath.Join(coreDir, ".venv", "Scripts", "python.exe"),
		filepath.Join(coreDir, ".venv", "bin", "python"),
	}
	for _, py := range candidates {
		if info, err := os.Stat(py); err == nil && !info.IsDir() {
			return py
		}
	}
	return ""
}

// BundledPython returns the interpreter shipped in the bundle next to the
// core directory (<bundle>/python, built by scripts/bundle.sh), or "" when
// the bundle has none. Its site-packages hold the locked dependencies; the
// core package itself is imported from <core>/src via PYTHONPATH (see
// CoreEnv), so the sources stay a plain, inspectable directory.
func BundledPython(coreDir string) string {
	root := filepath.Join(filepath.Dir(filepath.Clean(coreDir)), "python")
	candidates := []string{
		filepath.Join(root, "python.exe"),
		filepath.Join(root, "bin", "python3"),
	}
	for _, py := range candidates {
		if info, err := os.Stat(py); err == nil && !info.IsDir() {
			return py
		}
	}
	return ""
}

// CoreEnv is what a core started with the bundled interpreter needs in its
// environment: the sources on the path and nothing from a user's own
// Python (no user site-packages, no stray PYTHONHOME).
func CoreEnv(coreDir string) map[string]string {
	return map[string]string{
		"PYTHONPATH":       filepath.Join(coreDir, "src"),
		"PYTHONNOUSERSITE": "1",
		"PYTHONHOME":       "",
	}
}

// FindUV looks next to the launcher binary first (release bundle), then on
// PATH (developer machine).
func FindUV() string {
	name := "uv"
	if runtime.GOOS == "windows" {
		name = "uv.exe"
	}
	if exe, err := os.Executable(); err == nil {
		beside := filepath.Join(filepath.Dir(exe), name)
		if info, err := os.Stat(beside); err == nil && !info.IsDir() {
			return beside
		}
	}
	if path, err := exec.LookPath(name); err == nil {
		return path
	}
	return ""
}

// EnsureVenv creates <coreDir>/.venv from uv.lock when it is missing.
// Python is installed by uv under <dataDir>/python, its cache lives under
// <dataDir>/uv-cache - nothing is written outside the installation.
// Output goes to logOut so the person sees progress on first start.
func EnsureVenv(ctx context.Context, coreDir, dataDir string, logOut io.Writer) (string, error) {
	if py := BundledPython(coreDir); py != "" {
		return py, nil
	}
	if py := VenvPython(coreDir); py != "" {
		return py, nil
	}
	uv := FindUV()
	if uv == "" {
		return "", fmt.Errorf("no Python environment in %s and no uv found (next to the launcher or on PATH)", coreDir)
	}
	if _, err := os.Stat(filepath.Join(coreDir, "uv.lock")); err != nil {
		return "", fmt.Errorf("%s has no uv.lock - not a core directory", coreDir)
	}
	fmt.Fprintf(logOut, "first start: preparing the Python environment in %s (this needs the network once)\n", coreDir)
	cmd := exec.CommandContext(ctx, uv, "sync", "--frozen", "--no-dev", "--extra", "mcp")
	cmd.Dir = coreDir
	cmd.Env = append(os.Environ(),
		"UV_PYTHON_INSTALL_DIR="+filepath.Join(dataDir, "python"),
		"UV_CACHE_DIR="+filepath.Join(dataDir, "uv-cache"),
		"UV_PYTHON_PREFERENCE=only-managed",
	)
	cmd.Stdout = logOut
	cmd.Stderr = logOut
	if err := cmd.Run(); err != nil {
		return "", fmt.Errorf("uv sync failed: %w", err)
	}
	py := VenvPython(coreDir)
	if py == "" {
		return "", fmt.Errorf("uv sync finished but no interpreter appeared under %s/.venv", coreDir)
	}
	return py, nil
}
