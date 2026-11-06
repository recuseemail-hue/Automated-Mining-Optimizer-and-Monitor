from agent.platform.windows import LhmReader

TREE = {"Text": "Sensor", "ImageURL": "", "Children": [{"Text": "DESKTOP", "ImageURL": "images_icon/computer.png", "Children": [
    {"Text": "AMD Ryzen 3 3100", "ImageURL": "images_icon/cpu.png", "Children": [
        {"Text": "Temperatures", "ImageURL": "images_icon/temperature.png", "Children": [
            {"Text": "Core (Tctl/Tdie)", "Value": "52.4 °C", "Children": []}, {"Text": "CCD1 (Tdie)", "Value": "50.1 °C", "Children": []}]},
        {"Text": "Powers", "ImageURL": "images_icon/power.png", "Children": [{"Text": "Package", "Value": "41.3 W", "Children": []}]}]},
    {"Text": "NVIDIA GeForce RTX 3080", "ImageURL": "images_icon/nvidia.png", "Children": [
        {"Text": "Temperatures", "Children": [{"Text": "GPU Core", "Value": "67.0 °C", "Children": []},
                                             {"Text": "GPU Memory Junction", "Value": "72.0 °C", "Children": []}]},
        {"Text": "Fans", "Children": [{"Text": "GPU Fan 1", "Value": "2,180 RPM", "Children": []}]}]}]}]}


def test_lhm_matches_cpu_by_icon_and_gpu_memory_junction():
    r = LhmReader("http://x")
    r._tree, r._at, r.available = TREE, 1e12, True
    assert r.find("cpu", "Core (Tctl/Tdie)") == 52.4 and r.find("cpu", "Package") == 41.3
    assert r.gpu_memory_junction("RTX 3080", 0) == 72.0 and r.gpu_memory_junction("RTX 3080", 1) is None
    assert r.find("nvidia", "GPU Fan 1") == 2180.0 and r.gpu_fan_rpm(0) == 2180.0 and r.gpu_fan_rpm(1) is None
    assert LhmReader._value({"Value": "52,4 °C"}) == 52.4 and LhmReader._value({"Value": "1,234.5 MHz"}) == 1234.5
