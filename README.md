# Tania Dictée

> Dicte en québécois. Sans Dragon. Sans abonnement.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![Windows](https://img.shields.io/badge/Platform-Windows-lightgrey.svg)]()

Dictée vocale **locale** pour Windows — push-to-talk avec **F6**, transcription via Whisper en local, optimisée pour le français québécois et le franglais technique. Tout reste sur ta machine : zéro cloud, zéro compte, zéro abonnement.

---

<!-- DEMO GIF : ajoute ici ton screen-recording 30s (voir marketing/demo-storyboard.md si disponible) -->
<!-- Format recommandé : GIF 800×450px ou lien vers ta démo YouTube/mp4 — c'est le money shot qui convertit -->

---

## Ce que c'est

Tu maintiens **F6**, tu parles, tu relâches → ton texte apparaît là où ton curseur est déjà, dans n'importe quelle app (Telegram, Discord, Slack, Gmail, VS Code, Terminal, etc.).

- **100 % local** — le moteur [faster-whisper](https://github.com/SYSTRAN/faster-whisper) tourne sur ton GPU ou CPU, aucun appel réseau pendant la dictée
- **Franglais + code** — le glossaire personnel (`glossary.txt`) garde tes noms propres, termes techniques et mixtes FR/EN intacts
- **Pas de Dragon, pas d'abonnement** — open source MIT, installe une fois, utilise à vie
- **Windows-first** — hotkey globale F6 fonctionne dans toutes les apps, même en arrière-plan

---

## Installation rapide (recommandé)

**Télécharge l'installeur .exe** depuis la page Releases :

👉 **[github.com/elboKazQC/tania-dictee/releases/latest](https://github.com/elboKazQC/tania-dictee/releases/latest)**

- 63 MB · installation sans droits admin · Python non requis · modèle Whisper téléchargé automatiquement au 1er lancement
- Au 1er lancement : Windows SmartScreen peut afficher "Unknown publisher" → clique "More info" → "Run anyway" (comportement normal pour un binaire non signé, code signing à venir)

---

## Installation depuis le code source (pour devs)

**Prérequis :** Python 3.10+, Windows 10/11, microphone fonctionnel

```bat
git clone https://github.com/elboKazQC/tania-dictee.git
cd tania-dictee

:: Créer l'environnement virtuel
python -m venv .venv
.\.venv\Scripts\activate

:: Installer les dépendances
pip install -r requirements.txt

:: Lancer
python app.py
```

**GPU CUDA (optionnel — qualité max, transcription plus rapide) :**

```bat
:: Dans le même venv activé
pip install torch --index-url https://download.pytorch.org/whl/cu121
```

Au premier lancement, Whisper télécharge le modèle une seule fois (~200 MB pour `small` sur CPU, ~1.5 GB pour `large-v3` sur GPU). Ensuite c'est instantané.

---

## Utilisation

| Touche | Action |
|--------|--------|
| **F6 (maintenir)** | Enregistre tant que tu maintiens. Relâche → transcription + paste. |
| **Esc** | Annule l'enregistrement en cours — rien n'est collé. |
| **Ctrl+C** (terminal) | Quitte l'app. |

Personnaliser la hotkey :

```bash
python app.py --hotkey ctrl+alt+space
python app.py --hotkey f8
```

---

## Glossaire personnel

Édite `glossary.txt` — un terme par ligne. Whisper les traite comme des *hotwords* : tes noms propres, jargons techniques et marques arrêtent de se faire défigurer.

```
Anthropic
Cursor
mon-projet-secret
```

---

## Modes de cleanup

`--cleanup-mode` contrôle le post-traitement de la transcription :

| Mode | Comportement |
|------|-------------|
| `raw` | Sortie Whisper brute, rien touché |
| `soft` | Corrige les hotwords seulement |
| `gentle` *(défaut)* | soft + ponctuation normalisée + majuscule début de phrase |
| `llm` | Post-traitement via Ollama local (requiert [Ollama](https://ollama.ai) + `ollama pull mistral`) |

---

## Troubleshooting

**F6 ne déclenche rien** — une autre app a probablement capté la touche (logiciel gaming, OBS, Teams). Change la hotkey :
```bash
python app.py --hotkey ctrl+alt+space
```

**Micro pas détecté** — vérifie le micro par défaut dans Paramètres Windows > Son > Entrée :
```bash
python -c "import sounddevice as sd; print(sd.query_devices())"
```

**Antivirus flagge le module `keyboard`** — faux positif connu. Le module installe un hook clavier bas niveau, ce que certains AV interprètent comme un keylogger. C'est open source ([code source](https://github.com/boppreh/keyboard)). Whitelist le dossier `.venv`.

**CUDA détecté mais erreur au démarrage** — installe torch avec support CUDA ou force le CPU :
```bash
python app.py --device cpu
```

**Transcription lente** — modèle trop gros pour ton CPU. Passe à `small` ou `tiny` :
```bash
python app.py --model tiny
```

---

## Architecture

`app.py` contient tout (~1000 lignes, zéro dépendance exotique) :

- `Recorder` — capture audio sounddevice → wav 16 kHz mono
- `Transcriber` — faster-whisper avec VAD filter + initial prompt
- `Paster` — clipboard swap + Ctrl+V (ou typing fallback)
- `FeedbackOverlay` — toast tkinter qui pulse pendant l'enregistrement
- `TaniaDicteeApp` — state machine push-to-talk

---

## Version packagée (.exe)

Tu veux pas toucher à Python ? L'app packagée est dispo : installeur 1-clic, aucun setup, tu lances et tu parles.

👉 **App early access, 9 $ CAD une fois : [casaubon5.gumroad.com/l/symfz](https://casaubon5.gumroad.com/l/symfz)**

C'est de l'alpha assumée (Windows, pas signée, une console reste ouverte). Le code ci-dessus reste gratuit et MIT si tu préfères le faire tourner toi-même.

Tu veux plutôt l'installeur signé avec auto-update (à venir) ? Laisse ton courriel sur la [waitlist](https://elbokazqc.github.io/tania-dictee).

---

## Contribuer

PRs bienvenues. Ce qui manque et qui serait utile :

- Support macOS / Linux (actuellement Windows-only : `winsound`, `ctypes.windll`, `keyboard` qui requiert admin sur Linux)
- Voice activity detection auto (parle = enregistre, silence = stop)
- Diarization multi-locuteurs
- Hotkey globale même si une app a le focus exclusif (ex: jeux fullscreen)

---

## Licence

MIT — fais ce que tu veux. Fork, modifie, ship un produit dessus.
