#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

import binascii
import subprocess

from google.protobuf import text_format
import datetime
import pytz

from custom_components.googlefindmy.ProtoDecoders import DeviceUpdate_pb2, LocationReportsUpload_pb2


# Custom message formatter to print the Protobuf byte fields as hex strings
def custom_message_formatter(message, indent, as_one_line):
    lines = []
    indent = f"{indent}"
    indent = indent.removeprefix("0")

    for field, value in message.ListFields():
        if field.type == field.TYPE_BYTES:
            hex_value = binascii.hexlify(value).decode('utf-8')
            lines.append(f"{indent}{field.name}: \"{hex_value}\"")
        elif field.type == field.TYPE_MESSAGE:
            if field.label == field.LABEL_REPEATED:
                for sub_message in value:
                    if field.message_type.name == "Time":
                        # Convert Unix time to human-readable format
                        unix_time = sub_message.seconds
                        local_time = datetime.datetime.fromtimestamp(unix_time, pytz.timezone('Europe/Berlin'))
                        lines.append(f"{indent}{field.name} {{\n{indent}  {local_time}\n{indent}}}")
                    else:
                        nested_message = custom_message_formatter(sub_message, f"{indent}  ", as_one_line)
                        lines.append(f"{indent}{field.name} {{\n{nested_message}\n{indent}}}")
            else:
                if field.message_type.name == "Time":
                    # Convert Unix time to human-readable format
                    unix_time = value.seconds
                    local_time = datetime.datetime.fromtimestamp(unix_time, pytz.timezone('Europe/Berlin'))
                    lines.append(f"{indent}{field.name} {{\n{indent}  {local_time}\n{indent}}}")
                else:
                    nested_message = custom_message_formatter(value, f"{indent}  ", as_one_line)
                    lines.append(f"{indent}{field.name} {{\n{nested_message}\n{indent}}}")
        else:
            lines.append(f"{indent}{field.name}: {value}")
    return "\n".join(lines)


def parse_location_report_upload_protobuf(hex_string):
    location_reports = LocationReportsUpload_pb2.LocationReportsUpload()
    location_reports.ParseFromString(bytes.fromhex(hex_string))
    return location_reports


def parse_device_update_protobuf(hex_string):
    device_update = DeviceUpdate_pb2.DeviceUpdate()
    device_update.ParseFromString(bytes.fromhex(hex_string))
    return device_update


def parse_device_list_protobuf(hex_string):
    device_list = DeviceUpdate_pb2.DevicesList()
    device_list.ParseFromString(bytes.fromhex(hex_string))
    return device_list


def get_canonic_ids(device_list):
    result = []
    for device in device_list.deviceMetadata:
        if device.identifierInformation.type == DeviceUpdate_pb2.IDENTIFIER_ANDROID: 
            canonic_ids = device.identifierInformation.phoneInformation.canonicIds.canonicId
        else:
            canonic_ids = device.identifierInformation.canonicIds.canonicId
        device_name = device.userDefinedDeviceName
        for canonic_id in canonic_ids:
            result.append((device_name, canonic_id.id))
    return result


def get_devices_with_location(device_list):
    """Extract devices with location data from device list protobuf."""
    print("[DeviceDecoder] Using NEW location extraction from device list!")
    try:
        from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import decrypt_location_response_locations
        print("[DeviceDecoder] Successfully imported decrypt_location_response_locations")
    except Exception as e:
        print(f"[DeviceDecoder] Import error: {e}")
        return []
    
    result = []
    for device in device_list.deviceMetadata:
        # Get canonic IDs
        if device.identifierInformation.type == DeviceUpdate_pb2.IDENTIFIER_ANDROID: 
            canonic_ids = device.identifierInformation.phoneInformation.canonicIds.canonicId
        else:
            canonic_ids = device.identifierInformation.canonicIds.canonicId
        
        device_name = device.userDefinedDeviceName
        
        for canonic_id in canonic_ids:
            device_info = {
                "name": device_name,
                "id": canonic_id.id,
                "device_id": canonic_id.id,
                "latitude": None,
                "longitude": None,
                "altitude": None,
                "accuracy": None,
                "last_seen": None,
                "status": None,
                "is_own_report": None,
                "semantic_name": None,
                "battery_level": None
            }
            
            # Try to extract location data if available
            try:
                print(f"[DeviceDecoder] === Analyzing {device_name} ===")
                
                # Check what fields are actually available
                available_fields = []
                for field in device.DESCRIPTOR.fields:
                    if device.HasField(field.name):
                        available_fields.append(field.name)
                print(f"[DeviceDecoder] Available fields in device: {available_fields}")
                
                if device.HasField("information"):
                    info_fields = []
                    for field in device.information.DESCRIPTOR.fields:
                        if device.information.HasField(field.name):
                            info_fields.append(field.name)
                    print(f"[DeviceDecoder] Available fields in information: {info_fields}")
                    
                    if device.information.HasField("locationInformation"):
                        loc_fields = []
                        for field in device.information.locationInformation.DESCRIPTOR.fields:
                            if device.information.locationInformation.HasField(field.name):
                                loc_fields.append(field.name)
                        print(f"[DeviceDecoder] Available fields in locationInformation: {loc_fields}")
                        
                        if device.information.locationInformation.HasField("reports"):
                            print(f"[DeviceDecoder] Found location reports for {device_name}, attempting decryption...")
                            
                            # Create a fake device update protobuf with just this device's data
                            fake_device_update = DeviceUpdate_pb2.DeviceUpdate()
                            fake_device_update.deviceMetadata.CopyFrom(device)
                            
                            # Use existing decryption function
                            location_data = decrypt_location_response_locations(fake_device_update)
                            
                            if location_data and len(location_data) > 0:
                                print(f"[DeviceDecoder] Successfully decrypted {len(location_data)} locations for {device_name}")
                                # Use the most recent location (first in list)
                                latest_location = location_data[0]
                                device_info.update({
                                    "latitude": latest_location.get("latitude"),
                                    "longitude": latest_location.get("longitude"),
                                    "altitude": latest_location.get("altitude"),
                                    "accuracy": latest_location.get("accuracy"),
                                    "last_seen": latest_location.get("last_seen"),
                                    "status": latest_location.get("status"),
                                    "is_own_report": latest_location.get("is_own_report"),
                                    "semantic_name": latest_location.get("semantic_name")
                                })
                            else:
                                print(f"[DeviceDecoder] No location data returned from decryption for {device_name}")
                        else:
                            print(f"[DeviceDecoder] No 'reports' field found for {device_name}")
                    else:
                        print(f"[DeviceDecoder] No 'locationInformation' field found for {device_name}")
                else:
                    print(f"[DeviceDecoder] No 'information' field found for {device_name}")
                    
                print(f"[DeviceDecoder] === End analysis for {device_name} ===")
                print()
                        
            except Exception as e:
                print(f"[DeviceDecoder] Failed to extract location for device {device_name}: {e}")
                import traceback
                print(f"[DeviceDecoder] Traceback: {traceback.format_exc()}")
            
            result.append(device_info)
    
    return result


def print_location_report_upload_protobuf(hex_string):
    print(text_format.MessageToString(parse_location_report_upload_protobuf(hex_string), message_formatter=custom_message_formatter))


def print_device_update_protobuf(hex_string):
    print(text_format.MessageToString(parse_device_update_protobuf(hex_string), message_formatter=custom_message_formatter))


def print_device_list_protobuf(hex_string):
    print(text_format.MessageToString(parse_device_list_protobuf(hex_string), message_formatter=custom_message_formatter))


