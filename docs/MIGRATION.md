# SoulForge → Nous Agent Safety

Migration date: **2026-11-18**.

The existing public `LookJohnny/soulForge` repository is becoming `LookJohnny/nous-agent-safety`. This is the same repository, with its existing commit history, stars, forks, issues, and pull requests. Historical stars were earned across the repository's earlier work; they are not presented as endorsements of the current Nous implementation.

## Earlier SoulForge implementation

The previous default-branch version is preserved at:

- [Legacy branch: `legacy/soulforge-20261118`](https://github.com/LookJohnny/nous-agent-safety/tree/legacy/soulforge-20261118)
- [Original commit: `614645cf72277b39196441d91b9fe5e2931f9324`](https://github.com/LookJohnny/nous-agent-safety/tree/614645cf72277b39196441d91b9fe5e2931f9324)

That version contains the portable AI identity and companion-runtime implementation and its original licensing notices. Existing integrations should pin the historical commit when they need SoulForge's previous interfaces. The Nous default branch is a new product direction and does not promise compatibility with those interfaces.

## Public scope

The initial Nous publication is the local trajectory, privacy, review, and training-data quality toolkit under `colleague/`, together with its offline tests and synthetic examples. This is a developer alpha, with real-provider acceptance still incomplete.

Private research and patent documents, Lens research assets, model weights, hosted data infrastructure, real user conversations, credentials, local acceptance artifacts, and private Git history are excluded. The public source snapshot is committed on top of the existing public SoulForge history.

No blanket license grant is added by this migration. Earlier notices remain available with their historical code.

## Local clones

After the rename, update an existing clone's remote:

```bash
git remote set-url origin https://github.com/LookJohnny/nous-agent-safety.git
```

See [GitHub's repository rename documentation](https://docs.github.com/en/repositories/creating-and-managing-repositories/renaming-a-repository) for redirects and other rename behavior. Do not recreate the old repository name if you rely on those redirects.
