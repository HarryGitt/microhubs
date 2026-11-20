#!/usr/bin/env python3
"""Niels' reproducible OSM/terrain pipeline and scoring cross-check.

The script deliberately keeps official area_km2 as the denominator for every
density, matching the Excel workbook. OSM and terrain downloads are explicit;
missing terrain data is reported and never replaced by zero.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Iterable

import geopandas as gpd
import numpy as np
import pandas as pd
import requests
from shapely.geometry import LineString, Point, Polygon, shape
from shapely.ops import unary_union


OVERPASS_URL = os.getenv(
    "OVERPASS_URL",
    "https://api.overpass.ch/v1/interpreter",
)
PROJECTED_CRS = "EPSG:25832"
WGS84 = "EPSG:4326"
PARCELS_PER_RESIDENT = 4_370_000_000 * 0.6 / 83_600_000

CRITERIA = {
    "C1": ("parcels_proxy_per_km2", 1),
    "C2": ("households_per_km2", 1),
    "C3": ("residential_buildings_per_km2", 1),
    "C4": ("shops_per_km2", 1),
    "C5": ("establishments_per_km2", 1),
    "F1": ("mean_street_slope_pct", -1),
    "F2": ("hub_suitable_share_pct", 1),
    "F3": ("cycle_infra_km_per_km2", 1),
    "F4": ("restricted_network_pct", -1),
}

BASELINE = {
    "C1": 0.20,
    "C2": 0.10,
    "C3": 0.05,
    "C4": 0.075,
    "C5": 0.075,
    "F1": 0.15,
    "F2": 0.10,
    "F3": 0.15,
    "F4": 0.10,
}

SCENARIOS = {
    "S1_baseline": BASELINE,
    "S2_equal": {key: 1 / 9 for key in CRITERIA},
    "S3_demand_heavy": {
        "C1": 0.28, "C2": 0.14, "C3": 0.07, "C4": 0.105, "C5": 0.105,
        "F1": 0.09, "F2": 0.06, "F3": 0.09, "F4": 0.06,
    },
    "S4_feasibility_heavy": {
        "C1": 0.12, "C2": 0.06, "C3": 0.03, "C4": 0.045, "C5": 0.045,
        "F1": 0.21, "F2": 0.14, "F3": 0.21, "F4": 0.14,
    },
    "S5_baseline_without_C2": {
        **{key: value * 10 / 9 for key, value in BASELINE.items() if key != "C2"},
        "C2": 0.0,
    },
}


def normalise_name(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.lower().replace("ß", "ss"))
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = re.sub(r"\s*\(.*?\)", "", value)
    return re.sub(r"[^a-z0-9]+", "", value)


def overpass(query: str) -> dict:
    api_key = os.getenv("OVERPASS_KEY")
    headers = {"X-API-Key": api_key} if api_key else {}
    response = requests.post(
        OVERPASS_URL,
        data={"data": query},
        headers=headers,
        timeout=180,
    )
    response.raise_for_status()
    return response.json()


def element_geometry(element: dict):
    if element["type"] == "node":
        return Point(element["lon"], element["lat"])
    points = [(p["lon"], p["lat"]) for p in element.get("geometry", [])]
    if len(points) < 2:
        return None
    if element["type"] == "way":
        return LineString(points)
    return Polygon(points) if points[0] == points[-1] and len(points) >= 4 else None


def download_boundaries() -> gpd.GeoDataFrame:
    query = """
    [out:json][timeout:180];
    area["boundary"="administrative"]["admin_level"="6"]["name"="Frankfurt am Main"]->.city;
    rel(area.city)["boundary"="administrative"]["admin_level"="10"];
    out tags geom;
    """
    rows = []
    for element in overpass(query)["elements"]:
        geometry = element_geometry(element)
        name = element.get("tags", {}).get("name")
        if geometry is not None and name:
            rows.append({"osm_id": element["id"], "name": name, "geometry": geometry})
    if not rows:
        raise RuntimeError("Overpass returned no Frankfurt admin_level=10 boundaries")
    boundaries = gpd.GeoDataFrame(rows, crs=WGS84).to_crs(PROJECTED_CRS)

    airport = boundaries[boundaries["name"].map(normalise_name) == "flughafen"]
    south = boundaries[boundaries["name"].map(normalise_name) == "sachsenhausensud"]
    if not airport.empty and not south.empty:
        merged = unary_union([south.iloc[0].geometry, airport.iloc[0].geometry])
        boundaries = boundaries[
            ~boundaries["name"].map(normalise_name).isin({"flughafen", "sachsenhausensud"})
        ]
        boundaries = pd.concat(
            [
                boundaries,
                gpd.GeoDataFrame(
                    [{"osm_id": "merged-14", "name": "Sachsenhausen-Süd", "geometry": merged}],
                    crs=PROJECTED_CRS,
                ),
            ],
            ignore_index=True,
        )
    return boundaries


def download_features() -> tuple[
    gpd.GeoDataFrame,
    gpd.GeoDataFrame,
    gpd.GeoDataFrame,
    gpd.GeoDataFrame,
]:
    area = """
    area["boundary"="administrative"]["admin_level"="6"]["name"="Frankfurt am Main"]->.city;
    """
    shops_query = f"""
    [out:json][timeout:300]; {area}
    nwr(area.city)["shop"]; out center geom;
    """
    cycle_query = f"""
    [out:json][timeout:300]; {area}
    way(area.city)["highway"="cycleway"];
    way(area.city)["highway"]["cycleway"];
    way(area.city)["highway"]["bicycle"~"yes|designated"];
    out geom;
    """
    restricted_query = f"""
    [out:json][timeout:300]; {area}
    way(area.city)["highway"]["oneway"="yes"]
      ["cycleway"!~"opposite|contraflow|opposite_lane|opposite_track"];
    out geom;
    """
    streets_query = f"""
    [out:json][timeout:300]; {area}
    way(area.city)["highway"];
    out geom;
    """

    def to_gdf(payload: dict, geometry_types: Iterable[str]) -> gpd.GeoDataFrame:
        rows = []
        for element in payload["elements"]:
            geometry = element_geometry(element)
            if geometry is not None and geometry.geom_type in geometry_types:
                rows.append({"osm_id": element["id"], "tags": element.get("tags", {}), "geometry": geometry})
        frame = gpd.GeoDataFrame(rows, crs=WGS84).to_crs(PROJECTED_CRS)
        return frame.drop_duplicates("osm_id", keep="first").reset_index(drop=True)

    shops = to_gdf(overpass(shops_query), {"Point", "Polygon"})
    cycles = to_gdf(overpass(cycle_query), {"LineString"})
    restricted = to_gdf(overpass(restricted_query), {"LineString"})
    streets = to_gdf(overpass(streets_query), {"LineString"})
    return shops, cycles, restricted, streets


def assign_points(points: gpd.GeoDataFrame, boundaries: gpd.GeoDataFrame) -> pd.Series:
    if points.empty:
        return pd.Series(dtype="object")
    joined = gpd.sjoin(points[["geometry"]], boundaries[["name", "geometry"]], predicate="within", how="left")
    return joined["name"]


def line_lengths_by_district(
    lines: gpd.GeoDataFrame, boundaries: gpd.GeoDataFrame
) -> pd.Series:
    totals = {name: 0.0 for name in boundaries["name"]}
    if lines.empty:
        return pd.Series(totals)
    for district in boundaries.itertuples():
        clipped = lines.geometry.intersection(district.geometry)
        totals[district.name] = float(clipped.length.sum())
    return pd.Series(totals)


def slope_for_line(line: LineString, dataset, sample_spacing_m: float = 20.0) -> tuple[float, float]:
    if line.length < 1:
        return math.nan, 0.0
    distances = np.arange(0, line.length + sample_spacing_m, sample_spacing_m)
    distances[-1] = line.length
    points = [line.interpolate(float(distance)) for distance in distances]
    transformer = None
    if str(dataset.crs) != PROJECTED_CRS:
        from pyproj import Transformer
        transformer = Transformer.from_crs(PROJECTED_CRS, dataset.crs, always_xy=True)
    coords = [(p.x, p.y) for p in points]
    if transformer:
        coords = [transformer.transform(x, y) for x, y in coords]
    elevations = [value[0] for value in dataset.sample(coords)]
    nodata = dataset.nodata
    valid = [
        (a, b, d)
        for a, b, d in zip(elevations, elevations[1:], np.diff(distances))
        if d > 0 and not math.isnan(a) and not math.isnan(b) and a != nodata and b != nodata
    ]
    if not valid:
        return math.nan, 0.0
    weighted = sum(abs(b - a) / d * 100 * d for a, b, d in valid)
    length = sum(d for _, _, d in valid)
    return weighted / length, length


def district_slopes(streets: gpd.GeoDataFrame, boundaries: gpd.GeoDataFrame, dem_path: Path) -> pd.Series:
    import rasterio

    values = {name: [0.0, 0.0] for name in boundaries["name"]}
    with rasterio.open(dem_path) as dataset:
        for district in boundaries.itertuples():
            for line in streets.geometry.intersection(district.geometry):
                if line.is_empty:
                    continue
                segments = list(line.geoms) if line.geom_type == "MultiLineString" else [line]
                for segment in segments:
                    slope, length = slope_for_line(segment, dataset)
                    if not math.isnan(slope):
                        values[district.name][0] += slope * length
                        values[district.name][1] += length
    return pd.Series({
        name: total / length if length else math.nan
        for name, (total, length) in values.items()
    })


def validate_areas(boundaries: gpd.GeoDataFrame, districts: pd.DataFrame) -> pd.DataFrame:
    official = districts.set_index("stadtteil")["area_km2"]
    rows = []
    for district in boundaries.itertuples():
        name = district.name
        target = next(
            (key for key in official.index if normalise_name(key) == normalise_name(name)
             or (normalise_name(key).startswith("sachsenhausensud") and name == "Sachsenhausen-Süd")),
            None,
        )
        if target is None:
            rows.append({"stadtteil": name, "area_km2_official": math.nan, "area_km2_osm": district.geometry.area / 1e6, "within_2pct": False})
            continue
        osm_area = district.geometry.area / 1e6
        error = abs(osm_area / official[target] - 1)
        rows.append({"stadtteil": target, "area_km2_official": official[target], "area_km2_osm": osm_area, "within_2pct": error <= 0.02})
    result = pd.DataFrame(rows)
    matched = set(result.loc[result["area_km2_official"].notna(), "stadtteil"])
    missing = sorted(set(official.index) - matched)
    if len(result) != len(districts) or missing:
        raise ValueError(f"OSM boundary count/key check failed; missing: {', '.join(missing)}")
    if not result["within_2pct"].all():
        bad = result.loc[~result["within_2pct"], "stadtteil"].tolist()
        raise ValueError(f"OSM boundary area check failed (>2%): {', '.join(bad)}")
    return result


def join_features(
    districts: pd.DataFrame,
    boundaries: gpd.GeoDataFrame,
    shops: gpd.GeoDataFrame,
    cycles: gpd.GeoDataFrame,
    restricted: gpd.GeoDataFrame,
    streets: gpd.GeoDataFrame,
    slopes: pd.Series,
) -> pd.DataFrame:
    shops_by_name = assign_points(
        shops.set_geometry(shops.geometry.centroid) if not shops.empty else shops,
        boundaries,
    ).value_counts()
    cycle_length = line_lengths_by_district(cycles, boundaries) / 1000
    restricted_length = line_lengths_by_district(restricted, boundaries)
    all_streets = line_lengths_by_district(streets, boundaries)
    result = districts.copy()
    result["shops_osm"] = result["stadtteil"].map(shops_by_name).fillna(0).astype(int)
    result["cycle_infra_km_osm"] = result["stadtteil"].map(cycle_length).fillna(0.0)
    result["mean_street_slope_pct"] = result["stadtteil"].map(slopes)
    result["restricted_network_pct"] = (
        100 * result["stadtteil"].map(restricted_length) / result["stadtteil"].map(all_streets)
    )
    result["restricted_network_pct"] = result["restricted_network_pct"].replace([np.inf, -np.inf], np.nan)
    return result


def add_model_indicators(districts: pd.DataFrame) -> pd.DataFrame:
    result = districts.copy()
    area = pd.to_numeric(result["area_km2"], errors="coerce")
    built_up = (
        pd.to_numeric(result["residential_area_km2"], errors="coerce")
        + pd.to_numeric(result["commercial_mixed_area_km2"], errors="coerce")
        + pd.to_numeric(result["other_builtup_area_km2"], errors="coerce")
    )
    result["parcels_proxy_per_km2"] = (
        pd.to_numeric(result["population_2024"], errors="coerce")
        * PARCELS_PER_RESIDENT
        / area
    )
    result["households_per_km2"] = pd.to_numeric(result["households_2024"], errors="coerce") / area
    result["residential_buildings_per_km2"] = (
        pd.to_numeric(result["residential_buildings_2020"], errors="coerce") / area
    )
    result["establishments_per_km2"] = (
        pd.to_numeric(result["establishments_2018"], errors="coerce") / area
    )
    result["shops_per_km2"] = pd.to_numeric(result["shops_osm"], errors="coerce") / area
    result["hub_suitable_share_pct"] = 100 * (
        pd.to_numeric(result["commercial_mixed_area_km2"], errors="coerce")
        + pd.to_numeric(result["other_builtup_area_km2"], errors="coerce")
    ) / built_up
    result["cycle_infra_km_per_km2"] = (
        pd.to_numeric(result["cycle_infra_km_osm"], errors="coerce") / area
    )
    return result


def coverage_report(
    filled: pd.DataFrame,
    shops: gpd.GeoDataFrame,
    cycles: gpd.GeoDataFrame,
    restricted: gpd.GeoDataFrame,
    streets: gpd.GeoDataFrame,
) -> pd.DataFrame:
    columns = [
        ("shops_osm", len(shops)),
        ("cycle_infra_km_osm", len(cycles)),
        ("mean_street_slope_pct", int(filled["mean_street_slope_pct"].notna().sum())),
        ("restricted_network_pct", len(restricted)),
    ]
    return pd.DataFrame(
        [
            {
                "indicator": indicator,
                "source_feature_count": feature_count,
                "districts_with_value": int(filled[indicator].notna().sum()),
                "districts_with_zero": int((filled[indicator] == 0).sum()),
            }
            for indicator, feature_count in columns
        ]
        + [
            {
                "indicator": "street_network_denominator",
                "source_feature_count": len(streets),
                "districts_with_value": int((filled["restricted_network_pct"].notna()).sum()),
                "districts_with_zero": int((filled["restricted_network_pct"] == 0).sum()),
            }
        ]
    )


def score(districts: pd.DataFrame, weights: dict[str, float]) -> pd.DataFrame:
    result = districts.copy()
    included = result["include"].eq("Y")
    normalised = {}
    for criterion, (column, direction) in CRITERIA.items():
        values = pd.to_numeric(result[column], errors="coerce")
        available = included & values.notna()
        if not available.any():
            normalised[criterion] = pd.Series(np.nan, index=result.index)
            continue
        low, high = values[available].min(), values[available].max()
        if high == low:
            scaled = pd.Series(0.5, index=result.index)
        elif direction == 1:
            scaled = (values - low) / (high - low)
        else:
            scaled = (high - values) / (high - low)
        normalised[criterion] = scaled.where(available)
    numerator = sum(normalised[key].fillna(0) * weight for key, weight in weights.items())
    denominator = sum(normalised[key].notna() * weight for key, weight in weights.items())
    result["score"] = (100 * numerator / denominator).where(denominator > 0)
    result["rank"] = result.loc[included, "score"].rank(method="min", ascending=False).astype("Int64")
    for key, values in normalised.items():
        result[f"norm_{key}"] = values
    return result.sort_values(["rank", "stadtteil"], na_position="last")


def make_figures(scored: pd.DataFrame, boundaries: gpd.GeoDataFrame, output: Path) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError as error:
        raise RuntimeError(
            "The data pipeline completed, but figures could not be created because "
            "matplotlib/Pillow is not working in this environment. Repair the "
            "matplotlib and Pillow installation or rerun with --skip-figures."
        ) from error

    output.mkdir(parents=True, exist_ok=True)
    joined = boundaries.merge(scored, on="stadtteil", how="left")
    fig, ax = plt.subplots(figsize=(19.2, 10.8))
    joined.plot(column="score", ax=ax, legend=True, cmap="viridis", missing_kwds={"color": "lightgrey"})
    ax.set_axis_off()
    fig.savefig(output / "score_map.png", dpi=100, bbox_inches="tight")
    plt.close(fig)

    fig, axes = plt.subplots(3, 3, figsize=(19.2, 14), constrained_layout=True)
    for axis, criterion in zip(axes.flat, CRITERIA):
        joined.plot(column=f"norm_{criterion}", ax=axis, legend=False, cmap="viridis", missing_kwds={"color": "lightgrey"})
        axis.set_title(criterion)
        axis.set_axis_off()
    fig.savefig(output / "criteria_small_multiples.png", dpi=100)
    plt.close(fig)

    top = scored.dropna(subset=["score"]).nlargest(10, "score")
    fig, ax = plt.subplots(figsize=(19.2, 10.8))
    ax.barh(top["stadtteil"][::-1], top["score"][::-1], color="#4472c4")
    ax.set_xlabel("Score (0–100)")
    ax.set_title("Top 10 districts")
    fig.savefig(output / "top10_scores.png", dpi=100, bbox_inches="tight")
    plt.close(fig)


def run(args: argparse.Namespace) -> None:
    input_path = Path(args.input)
    output_dir = Path(args.output)
    districts = pd.read_csv(input_path)
    boundaries = download_boundaries()
    validate_areas(boundaries, districts).to_csv(output_dir / "boundary_check.csv", index=False)
    shops, cycles, restricted, streets = download_features()
    if not args.dem:
        raise ValueError("--dem is required for mean_street_slope_pct; no slope value is imputed")
    slopes = district_slopes(streets, boundaries, Path(args.dem))
    filled = join_features(districts, boundaries, shops, cycles, restricted, streets, slopes)
    filled = add_model_indicators(filled)
    filled.to_csv(output_dir / "district_indicators.csv", index=False)
    coverage_report(filled, shops, cycles, restricted, streets).to_csv(
        output_dir / "coverage_report.csv", index=False
    )

    scenario_dir = output_dir / "scenarios"
    scenario_dir.mkdir(parents=True, exist_ok=True)
    scenario_results = []
    for scenario, weights in SCENARIOS.items():
        result = score(filled, weights)
        result[["stadtteil", "score", "rank"]].to_csv(scenario_dir / f"{scenario}.csv", index=False)
        scenario_results.append(
            result.assign(scenario=scenario)[["scenario", "stadtteil", "rank", "score"]]
        )
    sensitivity = pd.concat(scenario_results, ignore_index=True)
    top3 = sensitivity.assign(top3=sensitivity["rank"].le(3)).groupby("stadtteil")["top3"].sum()
    sensitivity.to_csv(output_dir / "sensitivity.csv", index=False)
    top3.sort_values(ascending=False).rename("top3_scenarios").to_csv(output_dir / "top3_stability.csv")
    if not args.skip_figures:
        try:
            make_figures(score(filled, BASELINE), boundaries, output_dir / "figures")
        except RuntimeError as error:
            print(f"WARNING: {error}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    script_directory = Path(__file__).resolve().parent
    parser.add_argument(
        "--input",
        default=str(script_directory / "district_indicators_raw.csv"),
        help="District indicators CSV (default: beside this script)",
    )
    parser.add_argument(
        "--output",
        default=str(script_directory / "niels_output"),
        help="Output directory (default: niels_output beside this script)",
    )
    parser.add_argument("--dem", help="Local Hessen DGM1 GeoTIFF (required for slope)")
    parser.add_argument(
        "--skip-figures",
        action="store_true",
        help="Skip PNG creation; useful when matplotlib/Pillow is unavailable",
    )
    parser.add_argument(
        "--overpass-key",
        help="overpass.ch API key (prefer the OVERPASS_KEY environment variable)",
    )
    args = parser.parse_args()
    if args.overpass_key:
        os.environ["OVERPASS_KEY"] = args.overpass_key
    Path(args.output).mkdir(parents=True, exist_ok=True)
    run(args)


if __name__ == "__main__":
    main()
