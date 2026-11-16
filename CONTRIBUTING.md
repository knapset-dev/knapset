# Contributing to Knapset

Thank you for your interest in contributing to **Knapset**.
Our project is built on three core principles:
1. **Zero External Dependencies** — strictly Python Standard Library (no `pip install` required).
2. **High Performance** — sub-millisecond lookups without heavy framework overhead.
3. **Pluggable Modular Architecture** — add a single `.py` file to any subsystem to extend behavior.

---

## Building a Module

The project has **5 Decoupled Subsystems**. You can extend any of them by adding a Python file to the corresponding `modules/` directory:

| Subsystem | Directory | Blueprint Template | Purpose |
| :--- | :--- | :--- | :--- |
| **Market Information** | `info_providers/modules/` | [`template.py`](info_providers/modules/template.py) | Data adapters and normalizers for marketplace JSON feeds |
| **Scam & Defect Analyser** | `scam_analyser/modules/` | [`template.py`](scam_analyser/modules/template.py) | Category defect detection (GPUs, CPUs, Laptops, SSDs) |
| **Build Composer** | `build_composer/modules/` | [`template.py`](build_composer/modules/template.py) | Combinatorial set synthesizers (PC, Homelab, Audio) |
| **AI Inference Provider** | `ai_providers/modules/` | [`template.py`](ai_providers/modules/template.py) | LLM backends & agents via pure stdlib HTTPS |
| **Component Analyser** | `component_analyser/modules/` | [`template.py`](component_analyser/modules/template.py) | Hardware specs, benchmarks, and model identifiers |

### Step 1: Copy the Template
```bash
# Example: Adding a new marketplace data adapter
cp info_providers/modules/template.py info_providers/modules/custom_feed.py

# Example: Adding a new defect analyzer for SSDs
cp scam_analyser/modules/template.py scam_analyser/modules/ssd.py
```

### Step 2: Implement the Class
Subclass the subsystem base class (`BaseInfoProvider`, `BaseScamAnalyser`, etc.) and set your metadata:

```python
from scam_analyser.base import BaseScamAnalyser, ScamAnalysisResult

class SSDScamAnalyser(BaseScamAnalyser):
    name = "ssd"
    title = "Solid State Drive Defect Analyser"
    version = "1.0.0"
    author = "Your Name"
    description = "Detects fake controller firmware, degraded SMART wear, and re-badged flash."
    category = "defect_analysis"
    tags = ["ssd", "storage", "smart"]

    def analyze(self, item, detail=None, spec=None) -> ScamAnalysisResult:
        # Your defect detection rules here
        return ScamAnalysisResult(category="ssd", risk_score=10)
```

### Step 3: Verify Your Module
No configuration files need to be edited. Test your module immediately:

```bash
# Check that your module is auto-discovered:
python3 main.py modules

# Inspect marketplace JSON manifest output:
python3 main.py modules --json
```

---

## Testing Guidelines

Before opening a pull request, verify that all unit tests pass:

```bash
# Run the complete test suite:
python3 -m unittest discover tests
```

To add tests for your new module, create or extend a file in `tests/` using standard library `unittest`.

---

## Security & Code Standards

- **Standard Library Only**: Do not add third-party pip dependencies. Use `urllib.request`, `json`, `re`, `dataclasses`, and `concurrent.futures`.
- **Fault-Tolerant Loading**: Third-party plugins must never crash the core application. Broken plugins are isolated by registries and reported gracefully in `main.py modules`.
- **No Secrets**: Never commit API keys or private tokens. All credentials must be read from environment variables (e.g. `GEMINI_API_KEY`, `OPENAI_API_KEY`).
- **Path Portability**: Avoid hardcoded absolute file paths. Always use relative paths from the project root or `Path(__file__).resolve()`.

---

## Submitting a Pull Request

1. Fork the repository and create your branch: `git checkout -b feature/my-new-module`.
2. Commit your changes: `git commit -m "feat(scam): add SSD defect analyzer module"`.
3. Push to your fork: `git push origin feature/my-new-module`.
4. Open a Pull Request with a clear summary of your module.
