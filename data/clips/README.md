# ANPR sample clips

These four `.mp4` files are **placeholders** (0 bytes). The ingestion service
treats any file under ~1 KB as absent, so with only placeholders present it
runs in the `no_feed` health-event path.

Populate them with real or synthetic footage:

```bash
scripts/download_anpr_samples.sh
```

That tries CC dashcam sources and, failing those, generates 30s synthetic
clips via OpenCV. Provide direct CC URLs with:

```bash
ANPR_SAMPLE_URLS="https://.../a.mp4 https://.../b.mp4" scripts/download_anpr_samples.sh
```

Expected names: `cam_g1_entry.mp4`, `cam_g1_exit.mp4`,
`cam_corridor_km5.mp4`, `cam_corridor_km30.mp4`.
