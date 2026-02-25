"""Hardware detection for local model optimization.

Provides safe, aggregate system info (no serial numbers, paths, or process lists)
to help users understand if their hardware can run local AI models.
"""
import logging
import os
import platform
import shutil
import subprocess

import httpx

logger = logging.getLogger(__name__)


def get_hardware_info(ollama_base_url: str = "http://localhost:11434") -> dict:
    """Return aggregate hardware info for local model recommendations.

    Security: only exposes core count, RAM tier, GPU yes/no, and Ollama status.
    No serial numbers, no process lists, no filesystem paths.
    """
    info = {
        "cpu_cores": os.cpu_count() or 1,
        "ram_gb": _get_ram_gb(),
        "platform": platform.system().lower(),
        "arch": platform.machine(),
        "gpu": _detect_gpu(),
        "ollama": _check_ollama(ollama_base_url),
        "performance_tier": "unknown",
        "warnings": [],
    }

    # Determine performance tier and warnings
    ram = info["ram_gb"]
    has_gpu = info["gpu"]["detected"]
    cores = info["cpu_cores"]

    if ram >= 16 and has_gpu:
        info["performance_tier"] = "high"
    elif ram >= 16 or (ram >= 8 and has_gpu):
        info["performance_tier"] = "medium"
    elif ram >= 8:
        info["performance_tier"] = "low"
        info["warnings"].append(
            "No GPU detected. Ollama will use CPU only, which is slower for AI generation.")
    else:
        info["performance_tier"] = "minimal"
        info["warnings"].append(
            f"Only {ram:.0f}GB RAM detected. Local AI models may be very slow or run out of memory. "
            "Consider using a smaller model (e.g. tinyllama) or using the Claude API instead.")

    if cores <= 2:
        info["warnings"].append(
            f"Only {cores} CPU cores detected. AI generation will be slow.")

    if not info["ollama"]["running"]:
        info["warnings"].append(
            "Ollama is not running. Install it from https://ollama.com and run 'ollama serve' "
            "to enable free local AI recommendations.")

    # Suggest model based on hardware
    if ram >= 16:
        info["recommended_model"] = "llama3.1:8b"
    elif ram >= 8:
        info["recommended_model"] = "llama3.2:3b"
    elif ram >= 4:
        info["recommended_model"] = "tinyllama"
    else:
        info["recommended_model"] = "tinyllama"

    return info


def _get_ram_gb() -> float:
    """Get total system RAM in GB. Cross-platform."""
    try:
        system = platform.system()
        if system == "Windows":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            c_ulonglong = ctypes.c_ulonglong

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", c_ulonglong),
                    ("ullAvailPhys", c_ulonglong),
                    ("ullTotalPageFile", c_ulonglong),
                    ("ullAvailPageFile", c_ulonglong),
                    ("ullTotalVirtual", c_ulonglong),
                    ("ullAvailVirtual", c_ulonglong),
                    ("ullAvailExtendedVirtual", c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(stat)
            kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return stat.ullTotalPhys / (1024 ** 3)
        elif system == "Darwin":
            result = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return int(result.stdout.strip()) / (1024 ** 3)
        else:
            # Linux — read /proc/meminfo
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return kb / (1024 ** 2)
    except Exception:
        logger.debug("Could not detect RAM", exc_info=True)
    return 0


def _detect_gpu() -> dict:
    """Detect GPU availability. Only reports presence, not details."""
    result = {"detected": False, "type": "none"}

    # Check NVIDIA
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        try:
            proc = subprocess.run(
                [nvidia_smi, "--query-gpu=name", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10)
            if proc.returncode == 0 and proc.stdout.strip():
                result["detected"] = True
                result["type"] = "nvidia"
                return result
        except Exception:
            pass

    # Check AMD ROCm
    rocm_smi = shutil.which("rocm-smi")
    if rocm_smi:
        try:
            proc = subprocess.run(
                [rocm_smi, "--showproductname"],
                capture_output=True, text=True, timeout=10)
            if proc.returncode == 0:
                result["detected"] = True
                result["type"] = "amd"
                return result
        except Exception:
            pass

    # Check Apple Silicon (macOS with Metal)
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        result["detected"] = True
        result["type"] = "apple_silicon"
        return result

    return result


def _check_ollama(base_url: str) -> dict:
    """Check if Ollama is running and what models are available."""
    result = {
        "installed": False,
        "running": False,
        "models": [],
    }

    # Check if ollama binary exists
    result["installed"] = shutil.which("ollama") is not None

    # Check if server is responding
    try:
        resp = httpx.get(f"{base_url}/api/tags", timeout=3.0)
        if resp.status_code == 200:
            result["running"] = True
            data = resp.json()
            models = data.get("models", [])
            result["models"] = [m.get("name", "") for m in models]
    except Exception:
        pass

    return result
