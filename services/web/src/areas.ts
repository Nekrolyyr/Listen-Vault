// The research areas, as the api lists them (their names and order). Pages read the names from here, so a
// name is only written once, in the backend (listen/areas.py).
import { api } from "./api";

export interface AreaInfo {
  id: string;
  name: string;
}

let known: AreaInfo[] | null = null;

/** Every research area a paper can be filed in. Loaded once per page visit. */
export async function areaList(): Promise<AreaInfo[]> {
  if (!known) {
    const overview = await api.overview();
    known = overview.catalog;
  }
  return known;
}

/** The name of an area id; falls back to the id until the list has loaded. */
export function areaName(id: string): string {
  return known?.find((a) => a.id === id)?.name ?? id;
}

/** Forget the cached list (after the areas change shape, e.g. in tests). */
export function resetAreas(): void {
  known = null;
}
