# MY SOLANA TRADING DESK

Logiciel Windows personnel de copy trading sur Solana : 7 agents spécialisés et un JUDGE
déterministe qui a le dernier mot sur chaque transaction.

> **Version actuelle : 0.4 — moteur de données.**
> L'application s'installe, lit la blockchain, détecte les achats et ventes des traders suivis et
> teste toutes les connexions. **Elle n'achète ni ne vend rien pour l'instant** : les agents, le
> JUDGE et l'exécution arrivent par mises à jour, proposées directement dans l'application.

## Installer (utilisateur)

1. Ouvre la page **[Releases](https://github.com/apkiiii/solana-trading-desk/releases/latest)**
   et télécharge `SolanaTradingDesk-Setup-x.y.z.exe`.
2. Double-clique dessus. Windows peut afficher « Windows a protégé votre ordinateur » (le logiciel
   n'est pas signé par un certificat payant) : clique sur **Informations complémentaires**, puis
   **Exécuter quand même**.
3. Suis l'assistant (aucun droit administrateur nécessaire). Une icône est créée sur le bureau.
4. Dans l'application, menu **Clés API** : colle tes clés Helius et Jupiter, puis **Enregistrer**.
5. Menu **Diagnostic** : clique sur **Lancer le diagnostic** (tu peux y mettre l'adresse publique
   d'un trader repéré sur FOMO pour vérifier que ses trades sont détectés).

### Mises à jour

L'application vérifie au démarrage puis toutes les 6 heures s'il existe une nouvelle version.
Quand c'est le cas, le menu **Mises à jour** s'allume : lis les nouveautés, clique sur
**Télécharger et installer**. L'empreinte SHA-256 du fichier est vérifiée avant l'installation ;
l'application se ferme quelques secondes puis redémarre. Rien ne s'installe sans ton clic.

### Où sont mes données ?

`%LOCALAPPDATA%\SolanaTradingDesk` (base de données, journaux, sauvegardes, clés API).
Ce dossier est conservé lors des mises à jour et de la désinstallation.

## Avancement

| Version | Contenu | État |
|---|---|---|
| 0.4 | Application, moteur de données, mises à jour automatiques | ✅ |
| 0.5 | Agents SCOUT, RISK, WHALE, HEAD, SNIPER, EXIT | à venir |
| 0.6 | JUDGE (règles de sécurité déterministes) | à venir |
| 0.7 | Copy trading + exécution (SIMULATION / PAPER / LIVE) | à venir |
| 0.8 | Planificateur, DÉMARRER / STOPPER / ARRÊT D'URGENCE, moteur en arrière-plan | à venir |
| 0.9 | Interface complète (réglages, historique détaillé) | à venir |
| 1.0 | Batterie complète de tests obligatoires validée | à venir |

## Sécurité

- Mode **SIMULATION** par défaut ; le mode LIVE ne se réactive jamais tout seul après un redémarrage.
- Réglages modifiables uniquement depuis l'application, jamais par les agents.
- Le fichier de clés refuse toute clé privée ou phrase de récupération ; les clés sont masquées
  dans les journaux. La clé du wallet de trading sera stockée dans le Gestionnaire
  d'identifiants Windows (version 0.7).
- Doublons de transactions refusés par la base de données.
- Protège ton compte GitHub avec la double authentification : c'est lui qui publie les mises à jour.

## Développement

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest -q          # tests (aucun accès réseau)
.\.venv\Scripts\python.exe SolanaTradingDesk.py  # lancer l'application depuis le code
powershell -ExecutionPolicy Bypass -File packaging\build_exe.ps1   # fabriquer l'installateur
```

Publier une version : ajouter `packaging/release-notes/<version>.md` puis modifier le numéro dans
`core/version.py` et pousser sur `main` ; GitHub Actions teste, fabrique et publie l'installateur
(étiquette `v<version>` créée automatiquement). Relance possible depuis l'onglet Actions.

```
app/        application de bureau (PySide6)
config/     defaults.toml + réglages en lecture seule pour les agents
core/       chemins, journaux, version, mises à jour
data/       base SQLite, fournisseurs (RPC, Jupiter, DexScreener, RugCheck), détection des swaps,
            surveillance temps réel, données de marché
security/   clés API
tools/      diagnostic, base, traders (ligne de commande)
packaging/  fabrication du .exe et de l'installateur
tests/      tests automatiques
```

## Avertissement

Logiciel personnel expérimental. Le copy trading de memecoins est très risqué ; les filtres
réduisent certains risques sans les supprimer. Ce n'est pas un conseil financier.
