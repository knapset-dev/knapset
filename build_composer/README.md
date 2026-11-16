# PC Build Synthesizer Pack (`pc:builder`)

The **`pc:builder`** pack (registered under domain `pc` in the `build_composer` modular subsystem) is an engineering-grade combinatorial optimization engine for synthesizing fully compatible, electrically balanced, and bottleneck-free 5-part PC configurations:

$$\text{Build} = \langle \text{GPU}, \text{CPU}, \text{Motherboard}, \text{RAM}, \text{PSU} \rangle$$

It solves the multi-choice knapsack problem under any financial budget in **under 50 milliseconds** across tens of thousands of active marketplace listings.

---

## 1. Key Engineering Capabilities

1. **Deterministic Hardware Compatibility Engine (Zero Hallucination)**:
   - **Socket Matching**: Strict matching across modern and legacy platforms (`AM5`, `AM4`, `LGA1851`, `LGA1700`, `LGA1200`, `LGA1151v2`, `LGA1151`, `LGA2011-v3`).
   - **Memory Controller & Bus**: DDR4 vs DDR5 pinout and controller compatibility; memory slot saturation checks (enforces 2-stick kits on 2-DIMM chipsets like `H610`, `A520`, `A620`).
   - **VRM Thermal Safety vs CPU TDP**: Disqualifies configurations where a high-TDP processor (e.g. 250W Core i9-13900K or 170W Ryzen 9) would cause VRM throttling on budget 2-4 phase power deliveries.
   - **PSU Power & Cultists Safety Tiers**: Calculates combined transient loads, verifies ATX 3.0 / PCIe 5.0 12VHPWR requirements, and enforces safety headroom ($P_{\text{load}} \le 0.8 \times P_{\text{PSU}}$) with Cultists Tier list integration.
   - **CPU/GPU Balance Index**: Computes effective real-world 1080p/1440p framerates with bottleneck penalty damping, preventing unbalanced pairings (e.g. pairing an RTX 4090 with a Core i3).

2. **Condition & Warranty Transparency**:
   - Factory brand-new components (`[NEW]`) receive reliability score bonuses.
   - Certified refurbished / store outlet hardware (`[OUTLET]`) is tagged with retail warranty protection.
   - Secondary market used hardware (`used`) is validated via heuristic defect detectors and persistent SHA-256 AI audit ledgers.

3. **High-Speed Combinatorial Pruning**:
   - Per-model and per-tier **Pareto Frontier Pruning** filters out dominated, overpriced, or defective listings before the combinatorial loop.
   - 5-part build formulation evaluates thousands of candidates and ranks optimal finalists in **<50ms**.

---

## 2. Optimization Modes ('Mods')

The builder supports 15+ Pareto-weighted optimization profiles:

| Mode (`-M`) | Primary Focus | Best For |
|:---|:---|:---|
| **`best`** *(default)* | Balanced sweet-spot ($35\%$ perf, $25\%$ value, $25\%$ safety, $15\%$ future-proof) | Everyday gaming & general high-quality builds |
| **`value`** | Maximum gaming FPS per ₴ spent ($\ge 50\%$ weight on price efficiency) | Budget-conscious gamers seeking the maximum raw deal |
| **`fps`** / **`gaming`** | Maximum 1440p/4K GPU horsepower | Dedicated gaming rigs where GPU budget allocation is prioritized |
| **`safe`** | Tier-A/S VRMs, Tier-A Cultists PSUs, zero high-risk parts | Users prioritizing reliability, warranty, and low temperatures |
| **`future-proof`** | AM5 / LGA1851 platforms, DDR5 RAM, PCIe 5.0 readiness | Easy drop-in upgrades over the next 3–5 years |
| **`esports`** | High 1% low FPS, low true memory latency ($\le 10\text{ns}$), X3D L3 cache | Competitive fast-paced titles (CS2, Valorant, Apex, Warzone) |
| **`production`** | High CPU thread count ($16+$ threads) and high RAM capacity ($32-64\text{GB}$) | Video editing, 3D rendering, programming, and virtualization |
| **`deal-hunter`** | Outlier listings priced deep below secondary market medians | Bargain hunters seeking mispriced components |

---

## 3. CLI Quickstart & Verified Examples

Execute the PC builder directly from the root CLI:

### 3.1. Standard Budget Synthesis
Synthesize the 5 best balanced builds for 35,000 ₴:
```bash
python main.py build -b 35000
```

### 3.2. Pure Cost Efficiency (Value Mode)
Optimize for maximum FPS per UAH at a budget of 25,000 ₴:
```bash
python main.py build -b 25000 -M value
```

### 3.3. Locking Owned Components
If you already own an RTX 3080 and have a 15,000 ₴ budget to build the rest of the PC:
```bash
python main.py build -b 15000 --gpu "owned:RTX 3080"
```
*(Also supports `--cpu "owned:..."`, `--mobo "owned:..."`, `--ram "owned:..."`, `--psu "owned:..."`)*

### 3.4. Socket & Hardware Constraints
Lock the platform to AMD Socket AM5 with at least 16 CPU threads:
```bash
python main.py build -b 45000 --socket AM5 -t 16
```

### 3.5. Multi-Marketplace Synthesis
Include retail store catalogues (Telemart) and C2C classifieds (OLX) simultaneously:
```bash
python main.py build -b 30000 --provider all
```

### 3.6. AI Pre-Screening & Holistic System Audit
Filter suspicious listings using an LLM inspector before combinatorial synthesis:
```bash
python main.py build -b 40000 --ai-prescreen --ai-provider gemini
```

### 3.7. Interactive Human Validation Loop
Surface top GPU finalists and CPU+Platform proposals for interactive approval before assembling builds:
```bash
python main.py build -b 35000 --human-review
```

### 3.8. Exporting to JSON
Export generated builds into an automation-ready JSON report:
```bash
python main.py build -b 35000 --export build_result.json
```

---

## 4. Output Format Anatomy

When executed, the synthesizer produces clean, ANSI-colored output with OSC-8 terminal hyperlinks to the live marketplace listings:

```text
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Build #1: RX 6800 XT + RYZEN 5 5600X   │  23,700 ₴ (Score: 96.6) │  +6,300 ₴
Gaming: ~79 fps (1440p) / ~97 (1080p)  │  Load: ~409W on 750W    │  Mkt: -21% │ 1 New
──────────────────────────────────────────────────────────────────────────────────
  • GPU    RX 6800 XT            12,500 ₴ (-26%)  16GB             1440p:84fps
  • CPU    RYZEN 5 5600X         3,800 ₴ (-25%)   12T 32M          145/108idx
  • MOBO   MSI B550              3,200 ₴ (-27%)   AM4 DDR4         B-Tier [NEW]
  • RAM    Kingston Fury Beast   1,200 ₴ (-68%)   2x stick         10.0ns
  • PSU    Seasonic Focus 750W   3,000 ₴ (+40%)   750W             A-Tier
```

* **Header**: Build rank, primary pairing, total cost, composite score, budget surplus, estimated gaming FPS, power load, and market discount percentage.
* **Line Items**: Each part shows its negotiated price, discount vs used market median, hardware specs (VRAM, thread count, socket, RAM latency, PSU wattage), Cultists tier, and condition tag (`[NEW]`, `[OUTLET]`).

---

## 5. Programmatic Python API

The `pc:builder` pack can be invoked directly from Python applications:

```python
from build_composer.registry import get_composer, list_composers

# 1. Retrieve the PC composer via registry
composer = get_composer("pc")  # Aliases: "pc:builder", "pc_builder", "builder"

# 2. Synthesize builds under a 35,000 UAH budget
results = composer.compose(
    candidate_pool={},  # Empty dict auto-loads from local market cache
    budget=35000.0,
    mode="best",
    top_n=3,
)

for build in results:
    print(f"Build: {build.total_cost:,.0f} ₴ (Score: {build.score:.1f})")
    print(f"  GPU : {build.components['gpu']['model']} ({build.components['gpu']['price']} ₴)")
    print(f"  CPU : {build.components['cpu']['model']} ({build.components['cpu']['price']} ₴)")
    print(f"  FPS : ~{build.metrics['eff_fps_1440p']:.0f} fps (1440p)")
```

Alternatively, invoke `run_build_optimizer` directly from `build_optimizer`:

```python
from build_optimizer import run_build_optimizer

builds = run_build_optimizer(
    budget=40000.0,
    mode="value",
    socket_lock="AM5",
    top_builds=5,
)
```

---

## 6. Architecture & File Layout

```
knapset/
├── build_composer/
│   ├── base.py                   # BaseBuildComposer & ComposerSetResult contract
│   ├── registry.py               # Auto-discovery registry & alias resolver ("pc:builder" -> "pc")
│   ├── README.md                 # This documentation file
│   └── modules/
│       ├── pc_builder.py         # Modular adapter exposing PCBuildComposer
│       └── fishing.py            # Reference set composer for alternative domains
├── build_optimizer.py            # High-speed combinatorial solver & compatibility rules
├── modes.py                      # Multi-objective Pareto scoring profiles & weights
├── data/                         # Local market caches (*_market_cache.json)
└── main.py                       # Unified CLI dispatcher ('build' subcommand)
```
