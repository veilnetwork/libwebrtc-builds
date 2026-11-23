import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile


FETCH_SDK = Path(__file__).with_name('fetch-sdk.sh')
PIN = 'a' * 40
DEFAULT_TAG = f'webrtc-{PIN}'
PATCHED_TAG = f'{DEFAULT_TAG}-wasapi1'
REPO = 'example/sdk'
TARGET = 'win-x64'
BUNDLE = f'veil-webrtc-sdk-{TARGET}'
LIBRARY = Path('src/out/win-x64/obj/webrtc.lib')


class FetchSdkTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='veil-sdk-fetch-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.assets = self.root / 'assets'
        self.cache = self.root / 'cache'
        self.log = self.root / 'downloads'
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        curl = bin_dir / 'curl'
        curl.write_text(
            '#!/usr/bin/env python3\n'
            'import os, shutil, sys\n'
            'from pathlib import Path\n'
            'args = sys.argv[1:]\n'
            'url = args[-1].removeprefix("https://github.com/")\n'
            'with open(os.environ["MOCK_DOWNLOADS"], "a") as log:\n'
            '    log.write(url + "\\n")\n'
            'source = Path(os.environ["MOCK_ASSETS"]) / url\n'
            'if not source.is_file():\n'
            '    sys.exit(22)\n'
            'shutil.copyfile(source, args[args.index("-o") + 1])\n'
        )
        pwsh = bin_dir / 'pwsh'
        pwsh.write_text('#!/bin/sh\nexit 0\n')
        for program in (curl, pwsh):
            program.chmod(0o755)
        self.env = dict(
            os.environ,
            PATH=f'{bin_dir}{os.pathsep}{os.environ["PATH"]}',
            MOCK_ASSETS=str(self.assets),
            MOCK_DOWNLOADS=str(self.log),
        )
        self.publish(DEFAULT_TAG, b'original library')
        self.publish(PATCHED_TAG, b'patched library')

    def publish(self, tag, published_bytes, *, archived_bytes=None, pin=PIN,
                repo=REPO):
        release = self.assets / repo / 'releases/download' / tag
        release.mkdir(parents=True, exist_ok=True)

        def manifest(library):
            return json.dumps({
                'webrtc_pin': pin,
                'out_dir': 'out/win-x64',
                'libwebrtc_name': 'webrtc.lib',
                'libwebrtc_sha256': hashlib.sha256(library).hexdigest(),
            })

        (release / f'{BUNDLE}.json').write_text(manifest(published_bytes))
        if archived_bytes is None:
            archived_bytes = published_bytes
        with zipfile.ZipFile(release / f'{BUNDLE}.zip', 'w') as archive:
            archive.writestr(f'{BUNDLE}/veil-webrtc-sdk.json',
                             manifest(archived_bytes))
            archive.writestr(str(Path(BUNDLE) / LIBRARY), archived_bytes)
            archive.writestr(f'{BUNDLE}/setup.ps1', '# synthetic SDK fixture\n')
        return release

    def fetch(self, tag=None, *, repo=REPO, succeeds=True):
        args = ['bash', FETCH_SDK, '--target', TARGET, '--expect-pin', PIN,
                '--into', self.cache]
        if tag is not None:
            args += ['--tag', tag]
        result = subprocess.run(args, capture_output=True, text=True,
                                env=dict(self.env, VEIL_SDK_REPO=repo))
        if succeeds:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"WEBRTC_BUILD='{self.cache / BUNDLE}'", result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def library(self):
        return (self.cache / BUNDLE / LIBRARY).read_bytes()

    def test_reuse_same_release_without_downloading(self):
        self.fetch(PATCHED_TAG)
        self.assertEqual(self.library(), b'patched library')
        downloads = self.log.read_text()
        self.fetch(PATCHED_TAG)
        self.assertEqual(self.log.read_text(), downloads)

    def test_switch_releases_that_share_the_same_pin(self):
        self.fetch()
        self.fetch(PATCHED_TAG)
        self.assertEqual(self.library(), b'patched library')
        self.fetch()
        self.assertEqual(self.library(), b'original library')

    def test_switch_repositories_that_use_the_same_tag(self):
        self.publish(PATCHED_TAG, b'other repository', repo='example/other')
        self.fetch(PATCHED_TAG)
        self.fetch(PATCHED_TAG, repo='example/other')
        self.assertEqual(self.library(), b'other repository')

    def test_read_existing_cache_without_release_identity(self):
        release = self.publish(DEFAULT_TAG, b'legacy cache')
        with zipfile.ZipFile(release / f'{BUNDLE}.zip') as archive:
            archive.extractall(self.cache)
        self.fetch(PATCHED_TAG)
        self.assertEqual(self.library(), b'patched library')

    def test_keep_working_cache_when_release_manifest_disagrees_with_archive(self):
        self.fetch()
        self.publish(PATCHED_TAG, b'published library',
                     archived_bytes=b'other archive')
        result = self.fetch(PATCHED_TAG, succeeds=False)
        self.assertIn('archive manifest differs', result.stderr)
        self.assertEqual(self.library(), b'original library')

    def test_keep_working_cache_when_archive_library_has_wrong_checksum(self):
        self.fetch()
        release = self.publish(PATCHED_TAG, b'patched library')
        bundle = release / f'{BUNDLE}.zip'
        with zipfile.ZipFile(bundle) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        entries[str(Path(BUNDLE) / LIBRARY)] = b'corrupt library'
        with zipfile.ZipFile(bundle, 'w') as archive:
            for name, content in entries.items():
                archive.writestr(name, content)
        result = self.fetch(PATCHED_TAG, succeeds=False)
        self.assertIn('does not match the manifest checksum', result.stderr)
        self.assertEqual(self.library(), b'original library')

    def test_keep_working_cache_when_release_is_missing(self):
        self.fetch()
        self.fetch(f'{DEFAULT_TAG}-missing', succeeds=False)
        self.assertEqual(self.library(), b'original library')

    def test_reject_published_pin_mismatch_before_downloading_archive(self):
        self.fetch()
        self.publish(PATCHED_TAG, b'wrong pin', pin='b' * 40)
        self.fetch(PATCHED_TAG, succeeds=False)
        self.assertEqual(self.library(), b'original library')
        self.assertNotIn(f'{PATCHED_TAG}/{BUNDLE}.zip', self.log.read_text())


if __name__ == '__main__':
    unittest.main()
