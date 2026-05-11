from transit_data import SourceSpec, load_arcgis_source
import geopandas as gpd


def get_data(url, name, state="dc", category="poi", geometry_hint="auto"):
    spec = SourceSpec(
        name=name,
        url=url,
        state=state,
        category=category,
        geometry_hint=geometry_hint,
    )
    try:
        return load_arcgis_source(spec)
    except Exception as exc:
        print(f"Error fetching {name}: {exc}")
        return gpd.GeoDataFrame()
