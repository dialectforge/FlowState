# Releasing FlowState (signed tags)

`flowstate update` (2.4.0 and later) installs a release only if its git tag is **signed with an SSH
key listed in `mcp-server/flowstate/release_signers` of the copy the user already has**. It reads
that list before it changes anything, so a tag signed by anyone else (or not signed) is refused.
Without a key listed there, every `flowstate update` is refused with "no release signing key is
set up yet". Since 2.4.0 the list holds the FlowState release key (principal `flowstate-release`,
fingerprint `SHA256:3ZexAB20E6w1x7qWXivjtxhlqQ0RSBBuHNPilbiBzvE`), so the one-time part below is
done; it stays here for a new or replacement key. The commands are for Windows PowerShell; on a
Mac the `git` commands are the same, with `~/.ssh/flowstate_release_ed25519` paths. Nobody else
can do the one-time part for you: the private key must be made and kept by you.

## Two rules to keep in mind

1. **Trust on first use.** The first release that contains `flowstate update` (2.4.0) has to be
   installed by hand by everyone (`git pull`, then `setup_flowstate.py --verify`). The
   `release_signers` list in that release is the one later updates trust.
2. **A release must be signed by a key listed in the PREVIOUS release** (the one installed on
   the user's computer), not by a key the new release adds. To add or change a key: release N,
   signed with the old key, adds the new public key to `release_signers`; from release N+1 on you
   can sign with the new key. If you lose the only private key, users can't update with
   `flowstate update` until they install by hand a release that lists a new key.

## One time: make the signing key

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.ssh" | Out-Null
ssh-keygen -t ed25519 -C "flowstate-release" -f "$env:USERPROFILE\.ssh\flowstate_release_ed25519"
```

- Give it a passphrase when asked.
- The private key is `%USERPROFILE%\.ssh\flowstate_release_ed25519` (no extension). Keep it
  there. **Never put it in a repository, in the FlowState folder, or in a synced folder**
  (OneDrive, Dropbox, iCloud). Keep one offline copy (for example in your password manager or on
  a USB stick in a drawer).
- The public key is `flowstate_release_ed25519.pub`. Only this one goes anywhere else.

## One time: tell git to sign with it

In the clone you make releases from (these settings stay in that clone's `.git/config`):

```powershell
cd <the FlowState clone you release from>
git config gpg.format ssh
git config user.signingkey "$env:USERPROFILE\.ssh\flowstate_release_ed25519.pub"
git config gpg.ssh.allowedSignersFile "$PWD\mcp-server\flowstate\release_signers"
```

The last line only lets `git verify-tag` check your own tags locally.

## One time: list the public key in `release_signers`

```powershell
$pub = (Get-Content "$env:USERPROFILE\.ssh\flowstate_release_ed25519.pub" -Raw).Trim()
Add-Content -Encoding ascii mcp-server\flowstate\release_signers "flowstate-release namespaces=`"git`" $pub"
Get-Content mcp-server\flowstate\release_signers | Select-Object -Last 1
```

The last line must look like
`flowstate-release namespaces="git" ssh-ed25519 AAAAC3Nza... flowstate-release`
(`<principal> namespaces="git" <public key>`). Check that no private key is staged, then commit:

```powershell
git status
git add mcp-server\flowstate\release_signers
git commit -m "release_signers: add the FlowState release key"
```

## One time: on GitHub

- Add the public key as a **signing** key: GitHub, your picture, **Settings**, **SSH and GPG
  keys**, **New SSH key**, Key type **Signing Key**, paste the content of
  `flowstate_release_ed25519.pub`. GitHub then shows your tags as "Verified".
- Turn on **immutable releases** for `dialectforge/FlowState`: the repository's **Settings**,
  **General**, section **Releases**, tick **Enable release immutability**. A published release's
  tag can then no longer be moved or deleted.

## Every release

1. Set the version in both `mcp-server/pyproject.toml` (`version = "X.Y.Z"`) and
   `mcp-server/flowstate/__init__.py` (`__version__ = "X.Y.Z"`) and update `CHANGELOG.md`.
   `flowstate update` refuses a tag whose two files don't both say X.Y.Z. Commit.
2. Make the signed tag and check it:

   ```powershell
   git tag -s vX.Y.Z -m "FlowState X.Y.Z"
   git verify-tag vX.Y.Z
   ```

   `git verify-tag` must print `Good "git" signature for flowstate-release with ED25519 key ...`.
   If it doesn't, delete the local tag (`git tag -d vX.Y.Z`) and fix the setup before pushing.
3. Push the branch, then the tag:

   ```powershell
   git push origin main
   git push origin vX.Y.Z
   ```

4. On GitHub, publish a release from tag `vX.Y.Z` (not a draft, not a pre-release; the daily
   check only looks at the latest published release, and only at exact `vX.Y.Z` tags).
5. Try it on a plain clone of an older release (not your working copy):
   `<venv python> -m flowstate update --dry-run` must show `signed by flowstate-release`.
