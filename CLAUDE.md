# Octera-3V3 (pacote)

Fonte do que é instalado na Ender-3 V3 Plus. Na impressora fica em
`/usr/data/octera`; `config/` aparece como `config/Octera/`.

- Testes: `python tests/run_all.py` (requer `jinja2`).
- `square_probe_plan` em `klippy/extras/octera_mesh_reuse.py` deve prever
  exatamente a grade do macro `BED_MESH_CALIBRATE` em
  `config/kamp/Adaptive_Meshing.cfg`; `tests/test_probe_plan_mirror.py`
  garante isso. Mudou um, mude o outro.
- Grade sempre quadrada e ímpar; nunca retangular (o PRTouch falha).
- Nada gerado na impressora pode ficar dentro do repositório.
- Arquivos em `.cfg`, `.py` e `.sh` usam fim de linha LF.
- Repositório público: sem senhas, IPs, logs ou evidências. O histórico de
  engenharia e o acesso às impressoras ficam no repositório privado
  `octera-3v3`.
