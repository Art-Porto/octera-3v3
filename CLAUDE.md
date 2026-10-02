# Octera-3V3 (pacote)

Fonte do que é instalado na Ender-3 V3 Plus. Na impressora fica em
`/usr/data/octera`; `config/` aparece como `config/Octera/`.

- Testes: `python tests/run_all.py` (requer `jinja2`).
- `square_probe_plan` em `klippy/extras/octera_mesh_reuse.py` deve prever
  exatamente a grade do macro `BED_MESH_CALIBRATE` em
  `config/kamp/Adaptive_Meshing.cfg`; `tests/test_probe_plan_mirror.py`
  garante isso. Mudou um, mude o outro.
- Grade sempre quadrada; nunca retangular (o PRTouch falha). Grade só
  ímpar é opcional (`octera_odd_probe_count`).
- Mudança em `klippy/extras/*.py` só entra em memória com restart do
  **serviço** do Klipper (`POST /machine/services/restart?service=klipper`).
  O `RESTART`/`/printer/restart` mantém o processo e os módulos antigos.
  Conferir com `octera_mesh_reuse.module_sha256` no status do Klipper.
- Nada gerado na impressora pode ficar dentro do repositório.
- Arquivos em `.cfg`, `.py` e `.sh` usam fim de linha LF.
- Repositório público: sem senhas, IPs, logs ou evidências. O histórico de
  engenharia e o acesso às impressoras ficam no repositório privado
  `octera-3v3`.
