from pathlib import Path

from dogears import library_core


def test_find_ffmpeg_in_winget_links_when_not_on_path(tmp_path, monkeypatch):
    monkeypatch.setattr(library_core.shutil, 'which', lambda name: None)
    root = tmp_path / 'localappdata'
    link = root / 'Microsoft' / 'WinGet' / 'Links' / 'ffmpeg.exe'
    link.parent.mkdir(parents=True)
    link.write_bytes(b'binary')
    monkeypatch.setenv('LOCALAPPDATA', str(root))
    assert library_core.find_ffmpeg() == str(link)


def test_find_ffmpeg_in_winget_package_when_not_on_path(tmp_path, monkeypatch):
    monkeypatch.setattr(library_core.shutil, 'which', lambda name: None)
    root = tmp_path / 'localappdata'
    binary = (root / 'Microsoft' / 'WinGet' / 'Packages' / 'Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe'
              / 'ffmpeg-8.0-full_build' / 'bin' / 'ffmpeg.exe')
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b'binary')
    monkeypatch.setenv('LOCALAPPDATA', str(root))
    assert library_core.find_ffmpeg() == str(binary)
