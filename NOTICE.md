# Origem dos arquivos

Este pacote deriva do [Creality Helper Script](https://github.com/Guilouz/Creality-Helper-Script)
de Guilouz (GPL-3.0), commit `b46787a`, que por sua vez inclui o
[KAMP](https://github.com/kyleisah/Klipper-Adaptive-Meshing-Purging) de
kyleisah (GPL-3.0) e módulos de terceiros para o Klipper.

| Arquivo | Origem | Alteração |
|---|---|---|
| `config/kamp/Adaptive_Meshing.cfg` | KAMP / Helper Script | grade independente por eixo, square-max, grade ímpar opcional, estimativa de tempo |
| `config/kamp/Start_Print.cfg` | Helper Script (`Start_Print-3v3.cfg`) | dispatcher de reuso de malha |
| `config/kamp/KAMP_Settings.cfg` | Helper Script | parâmetros Octera e seção `[octera_mesh_reuse]` |
| `config/kamp/Line_Purge.cfg`, `Smart_Park.cfg` | KAMP / Helper Script | nenhuma |
| `config/macros/*`, `config/buzzer-support.cfg`, `config/camera-settings.cfg` | Helper Script | caminhos |
| `config/improved-shapers/*`, `files/delete_*.sh`, `files/ft2font*.so` | Helper Script | caminhos |
| `files/useful_macros.sh`, `files/beep.mp3` | Helper Script | nenhuma |
| `klippy/extras/gcode_shell_command.py`, `virtual_pins.py`, `calibrate_shaper_config.py` | Helper Script (terceiros) | nenhuma |
| `klippy/extras/octera_mesh_reuse.py`, `install.sh`, `tests/` | Octera | — |
