# ProspectIA — MVP

Prototype de dashboard de prospection pour les projets **levée de fonds** et **corners en entreprise**.

## Fonctions existantes
- Tableau de bord local de suivi des prospects.
- Recherche de **liens vers des profils LinkedIn publics indexés par des moteurs web**, via DDGS ; ne collecte pas directement LinkedIn.
- Ajout et suivi des prospects dans le navigateur, avec export.
- Préparation de messages ; intégration de brouillons Gmail avec PDF **non finalisée**.

> **Statut : prototype / tests en cours.** La recherche DDGS peut échouer selon les moteurs ou le réseau. L'automatisation de recherche, le remplissage complet des fiches, le scoring IA et le partage de données entre utilisateurs ne sont pas terminés.

## Lancer sur Mac
Avec Python 3.10+ installé :

```bash
cd ProspectIA-MVP
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python serveur.py
```

Puis ouvrir <http://127.0.0.1:8765/>. Le diagnostic est disponible sur <http://127.0.0.1:8765/api/health>.

## Lancer sur Windows
Extraire le dossier et double-cliquer sur `INSTALLER_ET_LANCER.bat`, puis ouvrir <http://127.0.0.1:8765/>.

## Sécurité et confidentialité
Ne pas committer `.venv`, des identifiants, des clés API, de vrais contacts ni des données confidentielles. Ne pas utiliser d'automatisation LinkedIn non autorisée. Respecter la réglementation sur les données personnelles et la prospection.

## Pour Charly
Points à examiner : fonctionnement du moteur DDGS, robustesse et sources, ingestion automatique des résultats, scoring, API Gmail pour brouillons avec pièces jointes, stockage partagé.
