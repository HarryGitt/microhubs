# Niels pipeline

The pipeline downloads Frankfurt's OSM `admin_level=10` boundaries and the
shop, cycle-infrastructure and one-way street layers, merges Flughafen into
Sachsenhausen-Süd, checks OSM areas against the official `area_km2` values,
and writes the four missing CSV columns. Street slope is calculated from a
local Hessen DGM1 GeoTIFF; it is never silently filled when terrain data is
missing.

Install the geospatial dependencies from `requirements-niels.txt`, obtain a
DGM1 GeoTIFF, then run:

```bash
python AlexDarmstadt/niels_pipeline.py \
  --dem /path/to/hessen_dgm1.tif \
  --output AlexDarmstadt/niels_output
```

If the data processing should run without the optional PNG output, use
`--skip-figures`. This avoids importing matplotlib during startup and is useful
when the local Pillow installation has a broken Windows DLL:

```bash
python AlexDarmstadt/niels_pipeline.py \
  --dem /path/to/hessen_dgm1.tif \
  --output AlexDarmstadt/niels_output \
  --skip-figures
```

The pipeline uses `https://api.overpass.ch/v1/interpreter` and accepts the
API key through the `OVERPASS_KEY` environment variable. On Windows PowerShell:

```powershell
$env:OVERPASS_KEY = "YOUR_API_KEY"
python Alex/niels_pipeline.py --dem "C:\Pfad\zu\hessen_dgm1.tif"
```

The key can also be passed once with `--overpass-key`; using the environment
variable is preferable because it avoids placing the key in shell history.

Outputs include `district_indicators.csv`, `coverage_report.csv`, the ±2% boundary check,
one ranking CSV per weight scenario, `sensitivity.csv`, `top3_stability.csv`,
and the three slide-ready PNG figures under `figures/`.

The scoring implementation follows the workbook: Min-Max normalization over
included districts, benefit/cost direction from `Weights`, and weight
rescaling over criteria that are available for a district.
