#!/usr/bin/env python3
"""Signs the sample registry in src/registry-sample again, with new throwaway keys.

Run it after changing the sample's index.json, revocations.json or a recipe. Every key, the
registry's and the moderation key's included, is made for this one run in a temporary folder and
deleted with it, so no real key ever signs the sample and nobody can sign for it again. The portal
tests sign the copies they change with the same code.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SAMPLE = HERE / "src" / "registry-sample"
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))
from tuxistant import registry  # noqa: E402

SIGNERS = "registry_allowed_signers"
SIGNERS_NOTE = """\
# SAMPLE. Not the registry's key: a throwaway key made by website/sample_registry.py to sign the
# sample registry, which only the website's local portal build shows. Its private half was deleted
# once the sample was signed, and Tux never ships it.
"""


class Keys:
    """Throwaway ed25519 keys, made in folder the first time each name is asked for."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.program = registry.keygen()
        if not self.program:
            raise SystemExit(registry.NO_KEYGEN)

    def public(self, name: str) -> str:
        """The key as the index writes it: "<type> <base64>", with no comment."""
        private = self.folder / name
        if not private.exists():
            self._run(["-q", "-t", "ed25519", "-N", "", "-C", "", "-f", str(private)])
        return " ".join(private.with_name(f"{name}.pub").read_text().split()[:2])

    def sign(self, name: str, namespace: str, data: bytes) -> bytes:
        self.public(name)
        return self._run(["-Y", "sign", "-q", "-f", str(self.folder / name), "-n", namespace],
                         data)

    def _run(self, argv: list, data: bytes = b"") -> bytes:
        return subprocess.run([self.program, *argv], input=data, capture_output=True, check=True,
                              env={"PATH": registry.KEYGEN_PATH, "HOME": str(self.folder),
                                   "LC_ALL": "C"}).stdout


def dumped(data) -> bytes:
    """As REGISTRY-FORMAT.md has the registry write its files."""
    return (json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def resign(folder: Path, keys: Keys) -> None:
    """Gives every author key in folder's index.json a throwaway key of keys, by login and place,
    and signs everything Tux checks with them: each recipe, with the .sig beside it, the index,
    with the moderation and registry keys named, and revocations.json. A version naming a key its
    author doesn't have, or a file that isn't there, is left as it is, for the checks to find."""
    index = json.loads((folder / "index.json").read_text())
    revoked = json.loads((folder / "revocations.json").read_text())
    renamed, signer = {}, {}
    for login, author in index["authors"].items():
        for place, entry in enumerate(author["keys"]):
            name = f"{login}-{place}"
            public = keys.public(name)
            fingerprint = registry.key_fingerprint(public, "sample key")
            renamed[entry["fingerprint"]] = fingerprint
            signer[fingerprint] = name
            entry.update(key=public, fingerprint=fingerprint)
    for skill in index["skills"]:
        for version in skill["versions"]:
            version["key"] = renamed.get(version["key"], version["key"])
            file = folder / "recipes" / f"{version['sha256']}.yaml"
            if version["key"] in signer and registry.SHA256.fullmatch(version["sha256"]) and (
                    file.is_file()):
                sig = keys.sign(signer[version["key"]], registry.RECIPE_NS, file.read_bytes())
                version["sig"] = sig.decode()
                file.with_name(f"{file.name}.sig").write_bytes(sig)
    for entry in index["revocations"] + revoked["revocations"]:
        if "key" in entry["scope"]:
            entry["scope"]["key"] = renamed.get(entry["scope"]["key"], entry["scope"]["key"])
    index["moderation_keys"] = [keys.public("moderation")]
    _signed(folder / "index.json", dumped(index), keys, "registry", registry.INDEX_NS)
    _signed(folder / "revocations.json", dumped(revoked), keys, "moderation",
            registry.REVOCATIONS_NS)
    (folder / SIGNERS).write_text(f'{SIGNERS_NOTE}{registry.REGISTRY} '
                                  f'namespaces="{registry.INDEX_NS}" {keys.public("registry")}\n')


def _signed(file: Path, data: bytes, keys: Keys, name: str, namespace: str) -> None:
    file.write_bytes(data)
    file.with_name(f"{file.name}.sig").write_bytes(keys.sign(name, namespace, data))


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="tux-sample-keys-") as folder:
        resign(SAMPLE, Keys(Path(folder)))
    print(f"signed {SAMPLE} with new throwaway keys, and deleted them")


if __name__ == "__main__":
    main()
