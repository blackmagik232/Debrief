# Signing Debrief releases

Debrief's in-app updater can check that an update really came from you. Each release is signed with
an Ed25519 key that only you hold. Installed copies carry the matching **public** key, and they won't
install an update whose signature doesn't check out.

**How it works today.** The verifier and the signing script have shipped since 2.2.2. They do nothing
until you set a key. With no key, `RELEASE_PUBKEYS` in `debrief/updater.py` is empty, and updates install
when their SHA-256 matches, as they always have. Once a key is set, every later update needs a valid
`.sig` file, and one without it is refused.

| Piece | Where |
|---|---|
| Release assets | `Debrief-Setup-<v>.exe`, `Debrief-Setup-<v>.exe.sha256`, `Debrief-Setup-<v>.exe.sig` |
| `.sig` file | one line holding the 64-byte signature as 128 hex characters (the updater also reads 88-character base64) |
| What gets signed (v1) | `Debrief release v1\n<version>\n<installer name>\n<sha256 hex, lowercase>\n` (ASCII). This binds the version, so an old release can't be served as a newer one. |
| Public keys in the app | `RELEASE_PUBKEYS = ("<64 hex>",)` in `debrief/updater.py`. It's a tuple so the key can be rotated. The 2.2.2 name `RELEASE_PUBLIC_KEY` still works. |
| Secret key | 64 hex characters (from `keygen`), or an Ed25519 PEM. It lives only in the GitHub secret `DEBRIEF_SIGNING_KEY` of the `release` environment, plus your offline backup. |
| Verifier | `debrief/ed25519.py`, pure Python (RFC 8032). `cryptography` is never part of Debrief.exe. |
| Signer | `packaging/sign_release.py`, which runs only in the CI release job and on your PC for `keygen`. |

## 1. Create the key pair once, on your own PC (not in CI)

From the repository folder, in a Command Prompt:

```
python packaging\sign_release.py keygen --out %USERPROFILE%\debrief-release-key.txt
```

This writes the **secret key** (64 hex characters) to that file and prints only the **public key** (also
64 hex characters). If the file already exists, it refuses to overwrite it. No extra packages are needed.

If you'd rather use OpenSSL 3, run `openssl genpkey -algorithm ed25519 -out debrief-release-key.pem`.
The PEM text then becomes the secret, and CI can read it because the release job installs
`cryptography`. To print its public key hex, set `DEBRIEF_SIGNING_KEY` to the PEM text and run
`python packaging\sign_release.py pubkey` (this needs `pip install cryptography` in a throwaway
virtualenv). You can also run
`openssl pkey -in debrief-release-key.pem -pubout -outform DER | tail -c 32 | xxd -p -c 64`.

## 2. Back up the secret offline

Copy the key file to a password manager or a USB stick, then delete it from the PC. If you lose it,
installed copies can't take signed updates until each person installs a new build by hand once.

## 3. Put the public key in the code

In `debrief/updater.py`:

```python
RELEASE_PUBKEYS = ("<the 64 hex characters keygen printed>",)
```

Keep the trailing comma. (A missing comma still works, but the comma keeps it a tuple.) Commit this.
A mistyped key never switches checking off: the app refuses every update and tells the person to
download from the release page.

## 4. Add the secret to GitHub

- Go to the repo's **Settings > Environments > New environment** and name it `release`.
- Add yourself under **Required reviewers**.
- Under **Deployment branches and tags**, limit it to `main` and tags `v*`.
- In that environment, add the secret **`DEBRIEF_SIGNING_KEY`**. Its value is the secret: either the 64
  hex characters, or the full PEM text including the BEGIN/END lines.
- Don't add it as a repository-wide secret. The `windows` build job runs third-party build tools and
  must never see it.

## 5. Release

Bump `__version__` in `debrief/__init__.py` and push to `main`, or push the tag `v<version>`.

1. The `windows` job runs the tests, the PyInstaller build, the smoke, upgrade and silent-install tests,
   then uploads the installer.
2. The `release` job waits for your approval in the **Actions** tab.
3. Once you approve, it does five things:
   - downloads the built installer;
   - installs a pinned `cryptography`;
   - writes the `.sha256`;
   - runs `python packaging/sign_release.py sign <exe> --version <v>`, which writes the `.sig` and checks
     it with Debrief's own verifier before it succeeds;
   - publishes the exe, the `.sha256` and the `.sig` with `gh release create`.

The secret is only ever an environment variable of the signing step. The script never prints it, and
nothing writes it to a file. A final `always()` step clears `$RUNNER_TEMP/debrief-signing-*` in case
a later change ever needs a key file.

CI guards against two mistakes:
- If `RELEASE_PUBKEYS` is set but the secret is missing, the release job fails rather than publishing
  an update installed copies would refuse.
- If the secret isn't one of the keys in `RELEASE_PUBKEYS`, `sign` refuses to sign.

**Transition.** The first version that ships with a key is the first that checks signatures. Copies
older than that update on SHA-256 alone, as before. That first version must itself be released signed,
and CI makes sure it is, so every update after it is verified.

## 6. Check it

On a PC with the previous signed version, go to **Settings > Updates > Check now > Update now**. The
update should install.

A release whose `.sig` is missing or unreadable should be refused with (`updater.NOT_SIGNED`):

> This update isn't signed by Debrief's release key, so it wasn't installed. Download it from the release page if you trust it.

A release whose `.sig` is there but doesn't verify (signed with another key, or signed for another
version, another file name or another file) should be refused with (`updater.BAD_SIGNATURE`):

> This update's signature doesn't match Debrief's release key, so it wasn't installed. Download it from the release page if you trust it.

(An installer whose file name doesn't match the release's version is refused before any signature
check, with "This release's installer doesn't match its version, so it wasn't installed.")

## 7. Rotating the key

1. Make a new pair (step 1), and add the new public key as the **second** item:
   `RELEASE_PUBKEYS = ("<old>", "<new>")`.
2. Release that version signed with the **old** key, so every installed copy accepts it.
3. Switch the `DEBRIEF_SIGNING_KEY` secret to the new key.
4. One release later, remove the old public key.

## 8. Never

- Never print the key in a workflow (no `echo`, no `set -x` around it).
- Never store the key in the repo or in an artifact.
- Never write the key to a file outside `$RUNNER_TEMP`.
- Never give the `windows` build job access to the `release` environment.
