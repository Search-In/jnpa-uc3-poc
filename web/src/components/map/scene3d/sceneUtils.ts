/**
 * sceneUtils — the smooth in-place FeatureLayer diff reused from the jnpa_poc_2
 * reference 3D engine (apps/web/src/map/layers.ts). Stable objectIds let the
 * FeatureLayerView UPDATE features in place (no whole-layer blink) as the live
 * UC3 truck feed refreshes, instead of delete-all + add-all.
 */
import type FeatureLayer from "@arcgis/core/layers/FeatureLayer";
import type Graphic from "@arcgis/core/Graphic";

/** Stable, deterministic objectId from a logical key (device_id, gate id, …). */
export function stableOid(key: string): number {
  let h = 5381;
  for (let i = 0; i < key.length; i++) h = ((h << 5) + h + key.charCodeAt(i)) | 0;
  return Math.abs(h) || 1;
}

/**
 * Reconcile a layer's features to `next` in a single applyEdits: shared ids are
 * UPDATED, new ones ADDED, gone ones DELETED. Because objectIds are stable per
 * asset, the FeatureLayerView transitions changed features in place.
 */
export async function applyGraphics(layer: FeatureLayer, next: Graphic[]): Promise<void> {
  const existing = await layer.queryFeatures();
  const oidField = layer.objectIdField;
  const prevByOid = new Map<number, Graphic>();
  for (const g of existing.features) prevByOid.set(g.attributes[oidField] as number, g);

  const addFeatures: Graphic[] = [];
  const updateFeatures: Graphic[] = [];
  const seen = new Set<number>();

  for (const g of next) {
    const id = g.attributes[oidField] as number;
    seen.add(id);
    const prev = prevByOid.get(id);
    if (!prev) addFeatures.push(g);
    else if (!attrsEqual(prev.attributes, g.attributes)) updateFeatures.push(g);
  }
  const deleteFeatures = existing.features.filter((g) => !seen.has(g.attributes[oidField] as number));

  if (!addFeatures.length && !updateFeatures.length && !deleteFeatures.length) return;
  await layer.applyEdits({ addFeatures, updateFeatures, deleteFeatures });
}

function attrsEqual(a: Record<string, unknown>, b: Record<string, unknown>): boolean {
  const keys = Object.keys(a);
  if (keys.length !== Object.keys(b).length) return false;
  for (const k of keys) if (a[k] !== b[k]) return false;
  return true;
}
