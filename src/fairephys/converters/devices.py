"""NWB devices with their DeviceModel (NWB 2.9+) from the metadata dict."""

from __future__ import annotations

from pynwb import NWBFile


def add_devices(nwbfile: NWBFile, metadata: dict) -> None:
    """Create every Ecephys device, with manufacturer and model on a linked DeviceModel.

    neuroconv writes only devices that an electrode group links to and puts the
    manufacturer on the device itself (deprecated in NWB 2.9). Devices created
    here first are reused by name, so the recording system is written too. A
    device without a manufacturer gets no model.
    """
    for dev in metadata.get("Ecephys", {}).get("Device", []):
        if dev["name"] in nwbfile.devices:
            continue
        model = None
        if manufacturer := dev.get("manufacturer"):
            model_name = dev.get("model") or dev["name"]
            model = (nwbfile.device_models[model_name] if model_name in nwbfile.device_models
                     else nwbfile.create_device_model(name=model_name, manufacturer=manufacturer,
                                                      description=dev.get("description")))
        nwbfile.create_device(name=dev["name"], description=dev.get("description"), model=model,
                              serial_number=dev.get("serial_number"))


def without_device_models(metadata: dict) -> dict:
    """Metadata for neuroconv: devices reduced to name and description."""
    ecephys = metadata.get("Ecephys", {})
    devices = [{"name": d["name"], "description": d.get("description", "")}
               for d in ecephys.get("Device", [])]
    return {**metadata, "Ecephys": {**ecephys, "Device": devices}}
