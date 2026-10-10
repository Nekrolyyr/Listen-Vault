# listen-vault

Private browser app that turns scientific papers and article URLs into audio.
See [PLAN.md](PLAN.md) for architecture, decisions, and progress.

This folder is a Dockge stack (`/home/doctor/docker/listen-vault`). `compose.yaml`
at the root defines all services and attaches to the shared `traefik` network.
