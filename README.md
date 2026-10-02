# Octera-3V3

Pacote de configuração para a Creality Ender-3 V3 Plus (F002) com Klipper,
Moonraker e Fluidd. Substitui o Creality Helper Script (descontinuado) pela
parte dele que esta impressora usa, mais as funções Octera.

## O que instala

| Componente | Origem |
|---|---|
| KAMP: malha adaptativa, purge, park e `START_PRINT` | KAMP / Helper Script, modificado |
| Reuso seguro de malha (`octera_mesh_reuse`) | Octera |
| M600, salvar Z-offset, macros úteis, buzzer, câmera | Helper Script |
| Shapers melhorados e gráficos | Helper Script |
| `gcode_shell_command`, `virtual_pins` | Helper Script |

Regras da malha adaptativa: a área segue a peça; a grade é sempre quadrada
(3 a 9 pontos por lado), porque o PRTouch desta impressora falha com grade
retangular. `octera_odd_probe_count: 1` restringe a grades ímpares. A malha medida é validada antes do print e uma
malha reprovada aborta antes de extrudar.

## Instalação

Impressora já com Moonraker atualizado (só a camada Octera):

    git clone https://github.com/Art-Porto/octera-3v3.git /usr/data/octera
    sh /usr/data/octera/install.sh --check   # mostra o que mudaria
    sh /usr/data/octera/install.sh

Impressora recém-restaurada de fábrica (base + camada Octera), com acesso
root habilitado e Wi-Fi configurado:

    git clone https://github.com/Art-Porto/octera-3v3.git /usr/data/octera
    sh /usr/data/octera/install-base.sh --check
    sh /usr/data/octera/install-base.sh

`install-base.sh` baixa o pacote base da release indicada em `base/RELEASE`
(Moonraker com ambiente Python pronto para MIPS e Fluidd), confere o
SHA-256, liga os serviços e chama `install.sh`. Não inclui OctoEverywhere
nem OctoApp, que têm instaladores próprios.

Depois reinicie o Klipper com a mesa livre. Os instaladores são idempotentes
e guardam backup de cada arquivo que editam em `/usr/data/octera-backups/`.
Atualizações chegam pelo Update Manager do Moonraker; quando uma atualização
adicionar arquivos novos, rode `install.sh` de novo.

Para desfazer, restaure os arquivos do backup e reinicie.

## Estrutura

| Pasta | Conteúdo |
|---|---|
| `config/` | fica visível na impressora como `config/Octera/`; `octera.cfg` é o único include |
| `klippy/extras/` | módulos do Klipper, ligados por link |
| `files/` | scripts e binários auxiliares |
| `base/` | serviço e configs do Moonraker/nginx usados por `install-base.sh` |
| `tools/` | geração do pacote base a partir de uma impressora funcionando |
| `tests/` | suítes offline (`python tests/run_all.py`, requer `jinja2`) |

Arquivos gerados na impressora ficam fora do repositório
(`octera-variables.cfg`, `octera-shapers/`), para o Update Manager não
marcar o pacote como modificado.

## Licença

GPL-3.0. Ver `NOTICE.md` para a origem de cada arquivo.
