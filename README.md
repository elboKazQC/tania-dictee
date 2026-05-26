# Tania Dictée

> Dicte en québécois. Sans Dragon. Sans abonnement.

Dictée vocale **locale** pour Windows — push-to-talk avec F6, transcription via Whisper en local, optimisée pour le français québécois (et le franglais qui va avec). Tout reste sur ta machine : zéro cloud, zéro compte, zéro abonnement.

## Ce que c'est

- Tu maintiens **F6**, tu parles, tu relâches → ton texte apparaît dans l'app active (Telegram, Discord, Slack, Gmail, VS Code, peu importe).
- Moteur : [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (Whisper compilé en C++/CTranslate2) en local, CPU ou GPU CUDA.
- Aucun appel réseau pendant la dictée. Le modèle Whisper se télécharge une fois au premier lancement.

## Quick start

```bash
git clone https://github.com/<your-fork>/tania-dictee.git
cd tania-dictee
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Premier lancement : Whisper télécharge le modèle (~200 MB pour `small`, ~1.5 GB pour `large-v3`). Ensuite c'est instantané.

## Hotkeys

| Touche | Action |
|--------|--------|
| **F6 (hold)** | Enregistre tant que tu maintiens. Relâche → transcription + paste. |
| **Esc** | Annule l'enregistrement en cours (clip jeté, pas de paste). |
| **Ctrl+C** (terminal) | Quitte l'app. |

Personnaliser la hotkey :
```bash
python app.py --hotkey ctrl+space
python app.py --hotkey f8
```

## Configuration

### Glossaire personnel

Édite `glossary.txt` — un terme par ligne. Whisper les utilise comme *hotwords*, donc tes noms propres, jargons, marques arrêtent de se faire mangler en transcription.

```
# Mes termes
Anthropic
Cursor
mon-projet-secret
```

### Modes de cleanup

`--cleanup-mode` contrôle le post-traitement de la transcription :

- `raw` — sortie Whisper brute, rien touché.
- `soft` — corrige les hotwords (Tania Dictée, Québec, IA, etc.) sans toucher au reste.
- `gentle` *(défaut)* — soft + ponctuation normalisée + majuscule début de phrase + point final.
- `llm` — pipe via Ollama local (par défaut `mistral`) pour un nettoyage plus intelligent. Nécessite [Ollama](https://ollama.ai) installé et un modèle pull (`ollama pull mistral`).

### Modèle Whisper

Auto-détecté selon ton hardware :
- **CPU** → `small` (~200 MB, ~2-4s pour 10s d'audio)
- **GPU CUDA** → `large-v3` (~1.5 GB, qualité max, plus rapide en GPU qu'un CPU avec small)

Override :
```bash
python app.py --model medium     # compromis qualité/vitesse
python app.py --model large-v3   # qualité max
python app.py --model tiny       # latence min, qualité faible
```

### Toutes les options

```bash
python app.py --help
```

## Troubleshooting

**1. "No audio captured" / micro pas détecté**
Vérifie ton micro par défaut dans Paramètres Windows > Son > Entrée. L'app utilise celui-là. Test rapide :
```bash
python -c "import sounddevice as sd; print(sd.query_devices())"
```

**2. F6 ne déclenche rien**
Une autre app a probablement capté la touche en premier (logiciel de gaming, OBS, Teams). Change la hotkey :
```bash
python app.py --hotkey ctrl+alt+space
```

**3. Antivirus flagge le module `keyboard`**
Faux positif connu — le module Python `keyboard` installe un hook bas niveau, ce que les AV interprètent parfois comme un keylogger. C'est open source, lis le code si tu veux ([source](https://github.com/boppreh/keyboard)). Whitelist le dossier `.venv` ou installe Tania Dictée hors d'un dossier protégé.

**4. CUDA détecté mais erreur au démarrage**
Tu n'as pas `torch` avec support CUDA. Soit installe-le (`pip install torch --index-url https://download.pytorch.org/whl/cu121`), soit force le CPU :
```bash
python app.py --device cpu
```

**5. Transcription lente / lag**
Modèle trop gros pour ton CPU. Passe à `small` ou `tiny` :
```bash
python app.py --model tiny
```

## Architecture (1 fichier)

`app.py` contient tout :
- `Recorder` — capture audio sounddevice → wav 16kHz mono
- `Transcriber` — faster-whisper avec VAD filter + initial prompt
- `Paster` — clipboard swap + Ctrl+V (ou typing fallback)
- `FeedbackOverlay` — toast tkinter qui pulse pendant l'enregistrement
- `TaniaDicteeApp` — state machine push-to-talk

~1000 lignes, zéro dépendance exotique, lisible d'un coup.

## License

MIT — fais ce que tu veux. Fork, modifie, ship un produit dessus, on s'en fout.

## Contributing

PRs welcome. Idées de features qui manquent :
- Support macOS / Linux (actuellement Windows-only à cause de `winsound`, `ctypes.windll`, et `keyboard` qui requires admin sur Linux)
- Voice activity detection auto (parle = enregistre, silence = stop)
- Multi-locuteurs (diarization)
- Hotkey en background même si une autre app a le focus exclusif
