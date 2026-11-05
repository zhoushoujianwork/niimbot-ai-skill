"""Exercise real archive bytes: deterministic output and refusal of links/leaks."""
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location("package_skill", Path(__file__).parents[1] / "scripts/package_skill.py")
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


class PackageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for relative in package.ALLOWLIST:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("fixture " + relative + "\n").encode())
        (self.root / "SKILL.md").write_text("---\nname: niimbot-label\ndescription: demo\n---\nBody\n")

    def test_deterministic_bytes_despite_source_metadata_changes(self):
        first, first_report = package.prepare(self.root)
        for relative in package.ALLOWLIST:
            path = self.root / relative
            os.utime(path, (2000000000, 2000000000))
            path.chmod(0o600)
        second, second_report = package.prepare(self.root)
        self.assertEqual(first, second)
        self.assertEqual(first_report, second_report)
        for data in second.values():
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                self.assertEqual(archive.namelist(), sorted(archive.namelist()))
                self.assertEqual(len(archive.namelist()), len(package.ALLOWLIST))
                for info in archive.infolist():
                    self.assertEqual(info.date_time, (1980, 1, 1, 0, 0, 0))
                    self.assertEqual(archive.read(info), archive.read(info.filename))

    def test_ignored_secrets_unlisted_and_adjacent_files_never_collected(self):
        sentinel = b"PRIVATE-SENTINEL-NOT-FOR-DISTRIBUTION"
        extras = (".env", "credentials.json", "jobs/private.json", "dist/old.zip", "scripts/unlisted.py", ".git/config", "publication/internal.json")
        for relative in extras:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(sentinel)
        (self.root / "unlisted-link").symlink_to(self.root / ".env")
        archives, report = package.prepare(self.root)
        for data in archives.values():
            self.assertNotIn(sentinel, data)
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                actual = set(archive.namelist())
                self.assertEqual(actual, {package.SLUG + "/" + p for p in package.ALLOWLIST})
        self.assertNotIn(sentinel.decode(), json.dumps(report))

    def test_symlink_file_directory_and_root_refused(self):
        source = self.root / "assets/preview.png"
        original = source.read_bytes()
        source.unlink()
        source.symlink_to(self.root / "README.md")
        with self.assertRaisesRegex(ValueError, "symlink"):
            package.prepare(self.root)
        source.unlink()
        source.write_bytes(original)
        assets = self.root / "assets"
        moved = self.root / "other-assets"
        assets.rename(moved)
        assets.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            package.prepare(self.root)
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            package.prepare(alias)

    def test_missing_allowlisted_file_fails_closed(self):
        (self.root / "LICENSE").unlink()
        with self.assertRaises(FileNotFoundError):
            package.prepare(self.root)

    def test_overlay_is_separate_and_does_not_modify_canonical(self):
        canonical = (self.root / "SKILL.md").read_bytes()
        archives, report = package.prepare(self.root)
        for filename, data in archives.items():
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                skill = archive.read(package.SLUG + "/SKILL.md")
                if filename.endswith("-agent-skill.zip"):
                    self.assertEqual(skill, canonical)
                else:
                    self.assertNotEqual(skill, canonical)
                    frontmatter = skill.decode().split("---")[1]
                    parsed = {line.partition(":")[0]: line.partition(":")[2].strip().strip('"')
                              for line in frontmatter.splitlines() if ":" in line}
                    for key, value in package.SKILLHUB_METADATA.items():
                        self.assertEqual(parsed[key], value)
                self.assertEqual(report["archives"][filename]["sha256"], package.sha256(data))
        self.assertEqual((self.root / "SKILL.md").read_bytes(), canonical)
        self.assertFalse((self.root / "dist").exists())


if __name__ == "__main__":
    unittest.main()
