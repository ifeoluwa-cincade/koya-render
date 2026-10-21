# render

Renders deterministic HTML scenes to video on GitHub Actions, split across parallel shards.

- `main` holds only the workflow (`.github/workflows/render.yml`) and the runner scripts (`farm/`).
- Each job pushes one encrypted bundle to a temporary `job/...` branch. The runners decrypt it with the `RENDER_KEY` secret, render, and upload only encrypted results. Branches and artifacts are deleted after the download.
- Logs print counts and timings only.
