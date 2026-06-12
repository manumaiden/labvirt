# vmbuilder

Avvia VM locali RHEL/Rocky/Debian per lab di testing in pochi secondi.

## OS Supportati

| Distro      | Versione                   |
|-------------|----------------------------|
| RHEL        | 7.9, 8.10, 9.7, 9.8, 10.2 |
| Rocky Linux | 7.9, 8.10, 9.8, 10.2      |
| Debian      | 12 (Bookworm), 13 (Trixie) |

## Requisiti

- Host Linux con KVM abilitato
- `libvirt`, `virt-install`, `virt-customize` (pacchetto `guestfs-tools`)
- `qemu-img`
- Python 3.6+
- Immagini qcow2 scaricate dal portale Red Hat / Rocky / Debian

## Installazione

```bash
cd ~/claudeproject/vmbuilder
./install.sh
source ~/.bashrc   # solo la prima volta, se ~/bin non era già in PATH
```

Crea un symlink `~/bin/vmbuilder` → `vmbuilder.py`. Dopo l'installazione il comando
è disponibile da qualsiasi directory.

## Utilizzo

### Menu interattivo

```bash
vmbuilder
```

Navigazione con frecce ↑↓, Invio per selezionare, `q` per uscire.

### CLI diretta

```bash
# Fase 1 — Costruire l'immagine golden (una volta per versione OS)
vmbuilder build --os rocky  --version 9.8  --src ~/downloads/Rocky-9.8-x86_64.qcow2
vmbuilder build --os rhel   --version 9.8  --src ~/downloads/rhel-9.8-x86_64.qcow2
vmbuilder build --os debian --version 12   --src ~/downloads/debian-12-amd64.qcow2

# Fase 2 — Creare una VM
vmbuilder create --os rocky --version 9.8 --name testlab-01
vmbuilder create --os rhel  --version 8.10 --name rhel-test --ram 4096 --vcpus 4

# Installazione fresh da ISO + kickstart (senza golden image)
vmbuilder ks-test --os rocky --version 9.8 \
  --iso ~/isos/Rocky-9.8-x86_64-dvd.iso \
  --ks kickstarts/ks_rocky9.cfg \
  --name ks-rocky9

# Lista VM (con IP management per VM running)
vmbuilder list

# Lista golden images in ORIGINAL_DIR
vmbuilder images

# Lista ISO files in KS_ISO_DIR
vmbuilder isos

# Distruggere una VM
vmbuilder destroy testlab-01
```

## Cosa viene applicato alle immagini golden (build)

| Operazione                        | RHEL | Rocky | Debian |
|-----------------------------------|:----:|:-----:|:------:|
| Root password (`Test1234!`)        | ✓    | ✓     | ✓      |
| Rimozione cloud-init              | ✓    | ✓     | ✓      |
| PermitRootLogin yes               | ✓    | ✓     | ✓      |
| Kernel arg NIC naming             | ✓    | ✓     | ✓      |
| bash-completion + vim             | ¹    | ✓     | ✓      |

¹ RHEL richiede subscription attiva — i pacchetti vanno installati dopo il boot.

## Topologia di rete (per ogni VM)

| NIC  | Rete    | IP                   | Scopo                   |
|------|---------|----------------------|-------------------------|
| NIC1 | default | 192.168.122.x (DHCP) | Management / SSH        |
| NIC2 | lab-net | 10.10.10.x (DHCP)    | Lab                     |
| NIC3 | lab-net | 10.10.10.x (DHCP)    | Bonding / teaming slave |
| NIC4 | lab-net | 10.10.10.x (DHCP)    | Bonding / teaming slave |

La rete `lab-net` (isolata, 10.10.10.0/24) viene creata automaticamente se non esiste.

## RHEL — Subscription automatica (opzionale)

```bash
export RHSM_USER="utente"
export RHSM_PASS="password"
vmbuilder create --os rhel --version 9.8 --name rhel-test
```

Con le variabili impostate, dopo il boot viene eseguita automaticamente via SSH
la registrazione + installazione di `bash-completion` e `vim`.

## Configurazione

`install.sh` copia automaticamente il config in `~/.vmbuilder.conf` alla prima installazione.
Tutte le modifiche vanno fatte lì — il file nel repo (`configs/lab.conf`) rimane intatto
anche dopo un `git pull`.

```bash
DEFAULT_RAM=2048          # RAM default per le VM (MB)
DEFAULT_VCPUS=2           # vCPU default
ROOT_PASSWORD="password"  # password root applicata al build

MGMT_NET="default"        # rete libvirt di management
LAB_NET="lab-net"         # rete libvirt di lab
LAB_NET_IP="10.10.10.1"
LAB_NET_DHCP_START="10.10.10.10"
LAB_NET_DHCP_END="10.10.10.200"

# Path per le immagini — supportano directory che richiedono sudo
# Lasciare vuoto = cartelle accanto allo script (original/ e vms/)
ORIGINAL_DIR="/percorso/golden"
VMS_DIR="/percorso/vms"

# Cartella scansionata per sorgenti qcow2 grezzi durante 'build'
# Se impostata, mostra un menu di selezione invece del prompt manuale
SOURCES_DIR="/percorso/sorgenti"

# Cartella scansionata per ISO durante 'ks-test'
# Se impostata, mostra un menu di selezione invece del prompt manuale
KS_ISO_DIR="/percorso/iso"

# Kickstart per 'ks-test':
#   directory → menu TUI dei *.cfg contenuti
#   file      → pre-riempie il prompt (e --ks in CLI)
#   vuoto     → chiede path manualmente
KS_PATH="/percorso/ks"

# Dimensione disco di default (GB) per VM create con ks-test
KS_DISK_SIZE=20
```

### Priorità config

1. `~/.vmbuilder.conf` — config utente (creato da `install.sh`)
2. `<repo>/configs/lab.conf` — fallback/template

### Storage su path privilegiati

Se `ORIGINAL_DIR` o `VMS_DIR` si trovano in directory che richiedono `sudo`
(es. mount point separati, `/mnt/...`, ecc.), lo script gestisce tutto in automatico —
tutte le operazioni su disco e rete vengono eseguite con `sudo`.

## Note su sudo

Lo script utilizza `sudo` internamente per tutte le operazioni privilegiate:

- `virsh` — gestione reti e VM
- `qemu-img` — creazione dischi linked clone
- `virt-install` / `virt-customize` — provisioning
- `mkdir` / `cp` / `rm` — operazioni sui path di storage

Non è necessario lanciare `vmbuilder` con `sudo` — il prefisso viene aggiunto
automaticamente dove serve.

## Struttura del progetto

```
vmbuilder/
├── vmbuilder.py        # Entry point unico (menu TUI + CLI)
├── install.sh          # Installa vmbuilder in ~/bin, crea ~/.vmbuilder.conf
├── configs/
│   └── lab.conf        # Template di configurazione (non modificare)
├── kickstarts/
│   ├── ks_rhel8.cfg    # Kickstart RHEL 8 — minimal server
│   ├── ks_rhel9.cfg    # Kickstart RHEL 9 — minimal server
│   ├── ks_rhel10.cfg   # Kickstart RHEL 10 — minimal server
│   ├── ks_rocky8.cfg   # Kickstart Rocky Linux 8 — minimal server
│   ├── ks_rocky9.cfg   # Kickstart Rocky Linux 9 — minimal server
│   └── ks_rocky10.cfg  # Kickstart Rocky Linux 10 — minimal server
├── original/           # Immagini golden provisioniate (default)
└── vms/                # Dischi VM clonati (default, creati a runtime)
```
