"""
Basler Camera Control Module

Detects, opens, configures and grabs frames from Basler cameras through
pypylon (the official Python wrapper of the Basler pylon SDK).

Key features:
- List every connected Basler camera (serial number, model, interface, nickname stored in the camera)
- Open one specific camera by serial number
- Load a .pfs configuration file into the camera
- Read the editable parameters, their limits, and apply new values in a safe order
- Grab frames for preview (latest frame only) or recording (every frame, with drop detection)

Parameter names differ between Basler camera generations:
- newer cameras (ace USB 3.0, ace 2, dart, boost...) use e.g. 'ExposureTime', 'Gain' (dB)
- older cameras (ace GigE "classic", Scout, Pilot, runner...) use e.g. 'ExposureTimeAbs',
  'GainRaw' (camera units, not dB)
The rest of the program always uses the newer names; this module finds, for each
camera, which name it really has (see PARAMETER_ALIASES). A parameter the camera
does not have at all is reported as None and skipped.
"""

from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from pypylon import genicam, pylon


# Parameters the user can change from the parameter window, in pylon node names.
EDITABLE_PARAMETERS = [
    'ExposureTime',
    'Gain',
    'AcquisitionFrameRate',
    'BinningHorizontal',
    'BinningVertical',
    'Width',
    'Height',
    'OffsetX',
    'OffsetY',
    'Gamma',
    'BlackLevel',
]

# Parameters that define the image geometry. The camera only accepts changes to
# them while it is NOT grabbing; all other editable parameters can change live.
GEOMETRY_PARAMETERS = ('BinningHorizontal', 'BinningVertical', 'Width', 'Height', 'OffsetX', 'OffsetY')

# Read-only parameters shown to the user and saved in the metadata.
INFO_PARAMETERS = [
    'PixelFormat',
    'ExposureAuto',
    'GainAuto',
    'AcquisitionFrameRateEnable',
    'BinningHorizontalMode',
    'BinningVerticalMode',
    'SensorWidth',
    'SensorHeight',
    'DeviceLinkThroughputLimitMode',
    'DeviceLinkThroughputLimit',
]

# Number of frame buffers pylon keeps while recording. If the program is
# briefly slower than the camera (e.g. disk hiccup), frames wait in these
# buffers instead of being dropped. 300 buffers = 5 s at 60 fps.
RECORDING_BUFFER_COUNT = 300

# Name used in this program -> names a camera may use for it, in order of preference.
# Names not listed here are the same on every camera.
PARAMETER_ALIASES = {
    'ExposureTime': ('ExposureTime', 'ExposureTimeAbs'),
    'Gain': ('Gain', 'GainAbs', 'GainRaw'),
    'AcquisitionFrameRate': ('AcquisitionFrameRate', 'AcquisitionFrameRateAbs'),
    'ResultingFrameRate': ('ResultingFrameRate', 'ResultingFrameRateAbs'),
    'BlackLevel': ('BlackLevel', 'BlackLevelAbs', 'BlackLevelRaw'),
}

# Camera parameter names whose values are NOT in the unit the program normally shows.
RAW_UNIT_NAMES = {'GainRaw': 'raw', 'BlackLevelRaw': 'raw'}

# pylon device class -> camera interface shown to the user.
INTERFACE_NAMES = {'BaslerUsb': 'USB3', 'BaslerGigE': 'GigE', 'BaslerCamEmu': 'emulator'}


class PfsMismatchError(ValueError):
    """A .pfs file was made for another type of Basler camera (e.g. USB3 file, GigE camera)."""


def interface_name(device_class: str) -> str:
    """'USB3', 'GigE', 'emulator', or the pylon device class itself if unknown."""
    return INTERFACE_NAMES.get(device_class, device_class or "unknown")


def pfs_interface(pfs_path: str) -> Tuple[str, str]:
    """
    Read the header of a .pfs file.

    Returns:
        (camera description written by pylon, e.g. 'Basler::UsbCameraParams',
         interface 'USB3' / 'GigE' / 'emulator', or '' if it cannot be recognised)
    """
    try:
        with open(pfs_path, 'r', encoding='utf-8', errors='replace') as f:
            for _ in range(5):
                line = f.readline()
                if line.startswith('# Device = '):
                    device = line[len('# Device = '):].split(' -- ')[0].strip()
                    lower = device.lower()
                    if 'usb' in lower:
                        return device, 'USB3'
                    if 'gige' in lower:
                        return device, 'GigE'
                    if 'camemu' in lower:
                        return device, 'emulator'
                    return device, ''
    except OSError:
        pass
    return '', ''


def list_basler_cameras() -> List[Dict[str, object]]:
    """
    List all Basler cameras connected to this PC.

    Returns:
        One dictionary per camera with:
        - 'serial': serial number (string)
        - 'model': model name (e.g. 'acA2040-120um')
        - 'interface': 'USB3', 'GigE', ... (see interface_name())
        - 'user_defined_name': nickname stored inside the camera (may be empty)
        - 'accessible': False when another program already has the camera open
    """
    factory = pylon.TlFactory.GetInstance()
    cameras = []
    for device in factory.EnumerateDevices():
        cameras.append({
            'serial': device.GetSerialNumber(),
            'model': device.GetModelName(),
            'interface': interface_name(device.GetDeviceClass()),
            'user_defined_name': device.GetUserDefinedName().strip(),
            'accessible': factory.IsDeviceAccessible(device),
        })
    return cameras


class BaslerCamera:
    """
    One Basler camera, identified by its serial number.

    The camera must be opened with open() before any other call, and should
    be released with close() when it is not needed anymore.
    """

    kind = "Basler"

    def __init__(self, serial: str):
        self.serial = serial
        self.camera: Optional[pylon.InstantCamera] = None
        # Converter used only if the camera does not deliver Mono8 directly.
        self._converter: Optional[pylon.ImageFormatConverter] = None

    # ------------------------------------------------------------------
    # Open / close
    # ------------------------------------------------------------------
    def open(self):
        """Open the camera. Raises RuntimeError with a clear message on failure."""
        factory = pylon.TlFactory.GetInstance()
        for device in factory.EnumerateDevices():
            if device.GetSerialNumber() == self.serial:
                if not factory.IsDeviceAccessible(device):
                    raise RuntimeError(
                        f"Basler camera {self.serial} is in use by another program "
                        "(e.g. pylon Viewer or another instance of this application)."
                    )
                self.camera = pylon.InstantCamera(factory.CreateDevice(device))
                self.camera.Open()
                return
        raise RuntimeError(f"Basler camera {self.serial} is not connected.")

    def is_open(self) -> bool:
        return self.camera is not None and self.camera.IsOpen()

    def close(self):
        """Stop grabbing (if needed) and close the camera."""
        if self.camera is None:
            return
        if self.camera.IsGrabbing():
            self.camera.StopGrabbing()
        if self.camera.IsOpen():
            self.camera.Close()
        self.camera = None

    @property
    def model_name(self) -> str:
        return self.camera.GetDeviceInfo().GetModelName() if self.camera else ""

    @property
    def interface(self) -> str:
        """'USB3', 'GigE', ... (see interface_name())."""
        return interface_name(self.camera.GetDeviceInfo().GetDeviceClass()) if self.camera else ""

    # ------------------------------------------------------------------
    # Parameter names (they differ between camera generations)
    # ------------------------------------------------------------------
    def _find(self, name: str):
        """
        Find a parameter on this camera.

        Args:
            name: name used in this program (e.g. 'ExposureTime')

        Returns:
            (name on this camera, parameter node), or (None, None) if the camera
            does not have this parameter (or it is not available right now).
            When the camera has several of the names, the first one that can be
            changed is used (e.g. 'GainRaw' if 'GainAbs' exists but is read-only),
            otherwise the first one that can be read.
        """
        readable = None
        for camera_name in PARAMETER_ALIASES.get(name, (name,)):
            try:
                node = getattr(self.camera, camera_name)
                if node is None or not genicam.IsAvailable(node) or not genicam.IsReadable(node):
                    continue
                if genicam.IsWritable(node):
                    return camera_name, node
                if readable is None:
                    readable = (camera_name, node)
            except Exception:
                continue  # This camera has no parameter with this name
        return readable if readable is not None else (None, None)

    def parameter_names(self) -> Dict[str, str]:
        """{name used in this program: name on this camera}, for the parameters the camera has."""
        names = {}
        for name in EDITABLE_PARAMETERS + INFO_PARAMETERS + ['ResultingFrameRate']:
            camera_name, _ = self._find(name)
            if camera_name is not None:
                names[name] = camera_name
        return names

    def units(self) -> Dict[str, str]:
        """
        Units that differ from the usual ones, e.g. {'Gain': 'raw'} when the camera
        only has 'GainRaw' (camera units) instead of a gain in dB.
        """
        units = {}
        for name in PARAMETER_ALIASES:
            camera_name, _ = self._find(name)
            if camera_name in RAW_UNIT_NAMES:
                units[name] = RAW_UNIT_NAMES[camera_name]
        return units

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    def load_pfs(self, pfs_path: str):
        """
        Load a .pfs configuration file (saved from pylon Viewer) into the camera.

        Raises:
            PfsMismatchError: the file was made for another type of camera (USB3 vs GigE...);
                        nothing is loaded in that case.
            Exception from pylon: a value in the file does not match this camera.
        """
        file_device, file_interface = pfs_interface(pfs_path)
        camera_interface = self.interface
        if file_interface and camera_interface in INTERFACE_NAMES.values() and file_interface != camera_interface:
            raise PfsMismatchError(
                f"{Path(pfs_path).name} was made for a {file_interface} Basler camera ({file_device}), "
                f"but this camera ({self.model_name}) is connected through {camera_interface}. "
                f"Nothing was loaded.")
        # The last argument (True) makes pylon validate every value it writes.
        pylon.FeaturePersistence.Load(pfs_path, self.camera.GetNodeMap(), True)

    def get_settings(self) -> Dict[str, object]:
        """
        Return the current value of every editable and informative parameter
        (None for parameters this camera does not have).
        """
        settings = {}
        for name in EDITABLE_PARAMETERS + INFO_PARAMETERS:
            _, node = self._find(name)
            try:
                settings[name] = node.GetValue() if node is not None else None
            except Exception:
                settings[name] = None
        return settings

    def get_limits(self) -> Dict[str, Tuple[float, float, float]]:
        """
        Return (minimum, maximum, increment) for every editable parameter that
        this camera has, as reported by the camera right now.

        Note: the limits of Width/Height/Offsets depend on binning and on each
        other; the parameter window recomputes them from the sensor size.
        (Binning and image size are read-only while the camera is grabbing, but they
        are still listed: apply_settings() is called with the grabbing stopped for them.)
        """
        limits = {}
        for name in EDITABLE_PARAMETERS:
            _, node = self._find(name)
            if node is None:
                continue  # Parameter not available on this camera model
            try:
                try:
                    increment = node.Inc
                except Exception:
                    increment = 0  # Float parameters without a fixed step
                limits[name] = (node.Min, node.Max, increment)
            except Exception:
                pass
        return limits

    def resulting_frame_rate(self) -> Optional[float]:
        """
        Frame rate the camera will really deliver with the current settings,
        or None if the camera does not report it.

        The camera computes it from exposure time, sensor readout time and USB
        bandwidth limit. It is lower than the requested frame rate when the
        settings do not allow the requested rate.
        """
        _, node = self._find('ResultingFrameRate')
        try:
            fps = float(node.GetValue()) if node is not None else None
        except Exception:
            return None
        return fps if fps and fps > 0 else None

    def _changed(self, settings: Dict[str, object]) -> Dict[str, object]:
        """The values of 'settings' that differ from the values currently on the camera."""
        current = self.get_settings()
        return {name: value for name, value in settings.items()
                if value is not None and current.get(name) != value}

    def changes_geometry(self, settings: Dict[str, object]) -> bool:
        """True if writing 'settings' changes binning, size or offsets (needs grabbing stopped)."""
        return any(name in GEOMETRY_PARAMETERS for name in self._changed(settings))

    def apply_settings(self, settings: Dict[str, object]) -> List[str]:
        """
        Write new parameter values to the camera. Only values that differ from
        the camera are written.

        If binning, size or offsets change, the camera must NOT be grabbing
        (check with changes_geometry()). The other parameters can be written
        while the preview is running.
        Values are written in an order that avoids range conflicts:
        offsets to 0 -> binning -> width/height -> offsets -> frame rate -> exposure/gain/etc.

        Args:
            settings: {pylon parameter name: value}. Unknown names are ignored.

        Returns:
            List of error messages (empty if every value was written).
        """
        errors = []
        current = self.get_settings()
        changed = self._changed(settings)

        def write(name, value):
            try:
                _, node = self._find(name)
                if node is None:
                    raise RuntimeError("this camera does not have this parameter")
                # Integer parameters must be a multiple of their increment (rounded down).
                if isinstance(node.GetValue(), int):
                    increment = max(int(node.Inc), 1)
                    value = int(node.Min + ((int(value) - node.Min) // increment) * increment)
                node.SetValue(value)
            except Exception as e:
                errors.append(f"{name} = {value}: {e}")

        if any(name in GEOMETRY_PARAMETERS for name in changed):
            # The whole geometry is rewritten (requested values, completed by the
            # current ones), because the offsets are reset to 0 first so that a
            # larger width/height is always allowed.
            geometry = {name: settings.get(name, current.get(name)) for name in GEOMETRY_PARAMETERS}
            for name in ('OffsetX', 'OffsetY'):
                _, node = self._find(name)
                if node is None:
                    continue  # No offsets on this camera
                try:
                    node.SetValue(0)
                except Exception as e:
                    errors.append(f"{name} reset: {e}")
            for name in GEOMETRY_PARAMETERS:
                if geometry[name] is not None:
                    write(name, geometry[name])

        # The frame rate is only respected if the frame rate limit is enabled.
        if (settings.get('AcquisitionFrameRate') is not None and current.get('AcquisitionFrameRate') is not None
                and not current.get('AcquisitionFrameRateEnable')):
            _, enable_node = self._find('AcquisitionFrameRateEnable')
            if enable_node is not None:
                try:
                    enable_node.SetValue(True)
                    changed['AcquisitionFrameRate'] = settings['AcquisitionFrameRate']
                except Exception as e:
                    errors.append(f"AcquisitionFrameRateEnable: {e}")
        # Older cameras only apply the gamma value when gamma correction is switched on.
        if 'Gamma' in changed:
            _, gamma_enable = self._find('GammaEnable')
            if gamma_enable is not None:
                try:
                    if not gamma_enable.GetValue():
                        gamma_enable.SetValue(True)
                except Exception as e:
                    errors.append(f"GammaEnable: {e}")
        for name in ('AcquisitionFrameRate', 'ExposureTime', 'Gain', 'Gamma', 'BlackLevel'):
            if name in changed:
                write(name, changed[name])

        return errors

    # ------------------------------------------------------------------
    # Frame grabbing
    # ------------------------------------------------------------------
    def start_grabbing(self, for_recording: bool):
        """
        Start continuous acquisition.

        Args:
            for_recording: True  -> keep every frame, in order (no frame is skipped
                                    on purpose; dropped frames can be counted).
                           False -> preview: always return the newest frame only.
        """
        self.camera.AcquisitionMode.SetValue('Continuous')
        pixel_format = self.camera.PixelFormat.GetValue()
        if pixel_format == 'Mono8':
            self._converter = None
        else:
            # Convert other formats to 8 bits, because MP4 files store 8-bit images.
            self._converter = pylon.ImageFormatConverter()
            if pixel_format.startswith('Mono'):
                self._converter.OutputPixelFormat = pylon.PixelType_Mono8
            else:
                self._converter.OutputPixelFormat = pylon.PixelType_BGR8packed

        if for_recording:
            self.camera.MaxNumBuffer.SetValue(RECORDING_BUFFER_COUNT)
            self.camera.StartGrabbing(pylon.GrabStrategy_OneByOne)
        else:
            self.camera.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)

    def stop_grabbing(self):
        if self.camera is not None and self.camera.IsGrabbing():
            self.camera.StopGrabbing()

    def grab(self, timeout_ms: int = 1000) -> Tuple[Optional[np.ndarray], Optional[int], bool]:
        """
        Wait for the next frame.

        Returns:
            (frame, block_id, incomplete)
            - frame: image as numpy array, or None if no complete frame arrived
            - block_id: frame counter set by the camera (used to detect dropped frames)
            - incomplete: True if the camera sent a frame that arrived damaged
        """
        result = self.camera.RetrieveResult(timeout_ms, pylon.TimeoutHandling_Return)
        if result is None or not result.IsValid():
            return None, None, False  # Timeout: no frame arrived
        try:
            block_id = result.GetBlockID()
            if not result.GrabSucceeded():
                return None, block_id, True
            if self._converter is None:
                frame = result.Array  # pypylon returns a copy, safe to keep
            else:
                frame = self._converter.Convert(result).Array
            return frame, block_id, False
        finally:
            result.Release()
