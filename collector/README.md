# collector

Second service of the `codereviewagent` monorepo (alongside [`reviewer/`](../reviewer)).

Currently a placeholder: `main.py` is a stub entrypoint so the CI pipeline can
build and push a `collector` image. Replace it with the real implementation.

## Build / run locally

```bash
docker build -t collector:dev collector
docker run --rm collector:dev
```

The image is built and pushed by the repo's `.gitlab-ci.yml` (see the
`collector build` / `collector push` jobs) on pushes to the default branch and
on git tags.
