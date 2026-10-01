from pathlib import Path

from omni_autofocus import cli, driver
from omni_autofocus.driver import State, UsbDevice

LASER_ID = ("USB\\VID_04B4&PID_1004&REV_0000", "USB\\VID_04B4&PID_1004")
OTHER = UsbDevice(("USB\\VID_046D&PID_C52B",), "Receiver", "HidUsb", 0)


def laser(service="CYUSB3", problem=0):
    return UsbDevice(LASER_ID, "Cypress FX2LP Sample Device", service, problem)


def test_diagnose_states():
    assert driver.diagnose([OTHER]).state is State.NO_LASER
    assert driver.diagnose([OTHER, laser()]).state is State.OK
    assert driver.diagnose([laser(service="", problem=28)]).state is State.NO_DRIVER
    assert driver.diagnose([laser(problem=28)]).state is State.NO_DRIVER
    wrong = driver.diagnose([laser(service="WINUSB")])
    assert wrong.state is State.WRONG_DRIVER and "'WINUSB'" in wrong.message
    assert driver.diagnose([laser(problem=10)]).state is State.PROBLEM


def test_guidance_points_to_commarker_studio_when_nothing_is_available():
    lines = driver.guidance(driver.diagnose([laser(service="")]), staged=False, installer=None)
    text = "\n".join(lines)
    assert driver.DOWNLOAD_URL in text and "ComMarker Studio" in text
    assert lines[-1] == "Run 'omni-autofocus driver' again to check."


def test_guidance_replug_when_driver_already_staged():
    text = "\n".join(driver.guidance(driver.diagnose([laser(service="")]), staged=True, installer=None))
    assert "Unplug" in text and driver.DOWNLOAD_URL not in text


def test_guidance_offers_local_installer():
    found = Path(r"C:\Program Files (x86)\ComMarker_Studio\CypressDriverInstaller.exe")
    text = "\n".join(driver.guidance(driver.diagnose([laser(service="")]), staged=False, installer=found))
    assert str(found) in text and driver.DOWNLOAD_URL not in text


def test_driver_staged_reads_inf(tmp_path):
    pkg = tmp_path / "cyusb3.inf_amd64_123"
    pkg.mkdir()
    (pkg / "cyusb3.inf").write_text("%VID_04B4&PID_1004.DeviceDesc%=CyUsb3, USB\\VID_04B4&PID_1004\n")
    assert driver.driver_staged(tmp_path)
    (pkg / "cyusb3.inf").write_text("USB\\VID_04B4&PID_00F3\n", encoding="utf-16")
    assert not driver.driver_staged(tmp_path)


def test_find_installers_searches_two_levels(tmp_path):
    (tmp_path / "Omni X" / "Driver").mkdir(parents=True)
    exe = tmp_path / "Omni X" / "Driver" / "CypressDriverInstaller.exe"
    exe.write_bytes(b"")
    assert exe in driver.find_installers([tmp_path])


def _fake_windows(monkeypatch, devices, installers=(), staged=False):
    monkeypatch.setattr(driver, "usb_devices", lambda: devices)
    monkeypatch.setattr(driver, "find_installers", lambda: list(installers))
    monkeypatch.setattr(driver, "driver_staged", lambda: staged)


def test_cli_driver_ok(monkeypatch, capsys):
    _fake_windows(monkeypatch, [laser()])
    assert cli.main(["driver"]) == 0
    assert "using the CyUSB3 driver" in capsys.readouterr().out


def test_cli_driver_runs_found_installer_and_rechecks(monkeypatch, capsys):
    found = Path("C:/x/CypressDriverInstaller.exe")
    states = iter([[laser(service="")], [laser()]])
    monkeypatch.setattr(driver, "usb_devices", lambda: next(states))
    monkeypatch.setattr(driver, "find_installers", lambda: [found])
    monkeypatch.setattr(driver, "driver_staged", lambda: False)
    ran = []
    monkeypatch.setattr(driver, "run_installer", lambda p: ran.append(p) or 0)
    monkeypatch.setattr(cli, "_prompt", lambda text: "")
    assert cli.main(["driver", "--yes"]) == 0
    assert ran == [found]
    assert "using the CyUSB3 driver" in capsys.readouterr().out


def test_cli_driver_without_installer_explains(monkeypatch, capsys):
    _fake_windows(monkeypatch, [laser(service="")])
    assert cli.main(["driver"]) == 1
    assert driver.DOWNLOAD_URL in capsys.readouterr().out


def test_no_device_message_mentions_driver_command(monkeypatch):
    monkeypatch.setattr(driver, "usb_devices", lambda: [laser(service="")])
    assert "omni-autofocus driver" in driver.no_device_message()
    monkeypatch.setattr(driver, "usb_devices", lambda: [OTHER])
    assert "switched on" in driver.no_device_message()
