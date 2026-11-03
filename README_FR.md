# ProspectIA Outreach — guide pas à pas

Ce module ajoute l'**envoi automatique sur LinkedIn** à ProspectIA, via **Unipile**.

Ce qu'il fait tout seul :

1. Il valide les prospects (score ≥ 7/10 **et** poste vérifié). Les autres restent en file « à vérifier ».
2. Il envoie l'invitation avec une note personnalisée.
3. Quand la personne accepte, il envoie le premier message, avec la mention de la source et « répondez STOP ».
4. Sans réponse, il relance à J+5 puis à J+10, puis s'arrête.
5. Dès que la personne répond, il stoppe la séquence. Si elle écrit « STOP », elle passe en liste « ne plus contacter », définitivement.

> ⚠️ **À savoir avant de commencer.** Automatiser l'envoi sur LinkedIn est contraire aux conditions d'utilisation de LinkedIn (§8.2). Le compte utilisé peut être restreint. Utilisez **un seul compte réel**, avec l'accord écrit de son propriétaire, et gardez les réglages prudents fournis.

---

## Étape 1 — Installer (une seule fois)

Ouvrez le Terminal sur votre Mac, allez dans le dossier du projet, puis tapez :

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Pour vérifier que tout fonctionne :

```bash
python -m pytest -q
```

Vous devez voir `29 passed`.

## Étape 2 — Unipile (version 2, tableau de bord dashboardv2.unipile.com)

1. Dans le tableau de bord Unipile, connectez **un seul** compte LinkedIn : celui qui enverra les messages.
2. Notez deux informations :
   - la **clé API**, dans le menu API keys ;
   - l'**identifiant du compte LinkedIn**, dans le menu Accounts. Il commence par `acc_`.

## Étape 3 — Remplir le fichier de réglages

```bash
cp .env.example .env      # ⚠️ une seule fois, sinon vos réglages sont effacés
open -e .env
```

Remplissez :

```
UNIPILE_VERSION=v2
UNIPILE_API_KEY=votre_clé
UNIPILE_ACCOUNT_ID=acc_xxxxxxxx
```

Laissez `WEBHOOK_SECRET` vide : il sera rempli automatiquement à l'étape 6. Laissez aussi `DRY_RUN=true` : dans ce mode, rien n'est envoyé.

Vérifiez la connexion. Cette commande ne fait que lire, elle n'envoie rien :

```bash
python -m prospectia_outreach check
python -m prospectia_outreach check votre-identifiant-linkedin   # fin de l'URL de votre profil
```

## Étape 4 — Importer les prospects

ProspectIA produit un fichier CSV avec ces colonnes (voir `exemple_prospects.csv`) :

| Colonne | Contenu |
|---|---|
| full_name, title, company | Identité et poste |
| project | `A` (mécénat) ou `B` (corners/partenariats) |
| score | Note sur 10 |
| role_verified | `oui` si le poste est confirmé par une source récente |
| linkedin_url | `https://www.linkedin.com/in/...` |
| source_label | D'où vient l'information, par ex. « rapport RSE 2025 » (cité dans le message) |
| channel | `invite` (par défaut) ou `inmail` (seulement avec Sales Navigator et `USE_INMAIL=true`) |
| note | Note d'invitation : 200 caractères au maximum, sinon la ligne part en « à vérifier » |
| first, followup1, followup2 | Premier message et les deux relances |

```bash
python -m prospectia_outreach import prospects.csv
```

Les doublons et les personnes en liste STOP sont ignorés automatiquement.

Vous pouvez aussi ajouter les prospects directement depuis votre code ProspectIA :

```python
from prospectia_outreach import db
conn = db.connect("prospectia_outreach.db")
db.add_prospect(conn, full_name="...", project="A", linkedin_url="...", score=8, role_verified=True,
                source_label="site de l'entreprise", messages={"note": "...", "first": "...",
                "followup1": "...", "followup2": "..."})
```

## Étape 5 — Tester en simulation

```bash
python -m prospectia_outreach run-once     # un passage
python -m prospectia_outreach status       # où en est-on ?
```

Les lignes `[SIMULATION]` montrent exactement ce qui *serait* envoyé. Relisez quelques messages avant d'aller plus loin.

## Étape 6 — Recevoir les notifications (acceptations et réponses)

Unipile prévient ProspectIA quand une invitation est acceptée ou qu'un message arrive. Il faut donc une adresse joignable depuis Internet.

1. **Premier Terminal**, lancez le récepteur :
   ```bash
   python -m prospectia_outreach webhook --port 8000
   ```
2. **Second Terminal**, ouvrez un tunnel gratuit avec Cloudflare. Installez-le une fois avec `brew install cloudflared`, puis :
   ```bash
   cloudflared tunnel --url http://localhost:8000
   ```
   Il affiche une adresse en `https://....trycloudflare.com`.
3. **Troisième Terminal**, dans le dossier du projet avec le `.venv` activé, déclarez cette adresse à Unipile :
   ```bash
   python -m prospectia_outreach setup-webhooks https://VOTRE-ADRESSE.trycloudflare.com
   ```
   Le **secret** renvoyé par Unipile est enregistré automatiquement dans `.env`.
4. Arrêtez le récepteur du premier Terminal (Ctrl + C) puis relancez-le, pour qu'il lise ce secret.

Chaque notification reçue est copiée dans `webhooks_recus.jsonl`. Envoyez ce fichier si quelque chose ne réagit pas comme prévu, car le format des notifications v2 est encore peu documenté par Unipile.

> Un tunnel rapide change d'adresse à chaque relance : il faut alors refaire le point 3. Supprimez l'ancien webhook dans le tableau de bord Unipile (menu Webhooks). L'ordinateur doit rester allumé pendant les heures d'envoi.

## Étape 7 — Passer en réel

1. Dans `.env`, mettez `DRY_RUN=false`.
2. Lancez le moteur :
   ```bash
   python -m prospectia_outreach run --every 15
   ```

Il tourne environ toutes les 15 minutes et fait au maximum 3 actions par passage, espacées de 1,5 à 5 minutes. Il ne fonctionne que du lundi au vendredi, de 9 h à 18 h.

## Chercher des prospects sur LinkedIn (lecture seule)

**Le plus simple, depuis le tableau de bord** : carte « Chercher sur LinkedIn ».
1. Tapez des mots-clés, puis cliquez sur **Rechercher**.
2. Cochez les profils qui vous intéressent, puis cliquez sur **Ajouter la sélection**.
3. Sur chaque nouveau prospect, cliquez sur **Écrire les messages** : note, 1er message, relances, score, poste vérifié. Puis **Enregistrer**.
4. Cliquez sur **Valider** : le prospect partira au prochain passage.

Un prospect sans 1er message ne peut pas être validé, et le moteur ne l'enverra jamais.

**Ou en ligne de commande :**

1. Sur linkedin.com, faites une recherche de **Personnes** avec vos filtres : mots-clés (« directeur RSE », « mécénat »…), lieu (Île-de-France), etc.
2. Copiez l'adresse de la page de résultats. Elle contient `linkedin.com/search/results/people/`.
3. Dans le Terminal, collez-la **entre guillemets** :
   ```bash
   python -m prospectia_outreach search --url "https://www.linkedin.com/search/results/people/?..." --limit 25 --project A
   ```
   Pour une recherche simple, sans filtres : `--keywords "directeur RSE"`.

Le programme affiche les profils trouvés et crée un fichier `resultats_recherche_AAAAMMJJ_HHMM.csv`. **Rien n'est envoyé.** Les doublons, les profils masqués et les personnes en liste STOP sont écartés.

Ces lignes n'ont ni message ni score. Si vous les importez telles quelles, elles restent « à vérifier ». Il faut d'abord que ProspectIA (ou vous) note chaque profil, vérifie le poste et rédige les messages.

Limites : 10 recherches par jour au maximum (`MAX_SEARCHES_PER_DAY`). Avec un compte LinkedIn gratuit, LinkedIn limite aussi le nombre de recherches par mois.

## Le tableau de bord (dans le navigateur)

```bash
python -m prospectia_outreach dashboard
```

La page s'ouvre toute seule sur http://127.0.0.1:8001. Elle n'est visible que sur votre ordinateur. Vous y trouvez :

- les chiffres clés : prospects, invitations, taux d'acceptation, taux de réponse, STOP ;
- les **réponses à traiter**, avec un lien direct pour répondre sur LinkedIn ;
- la liste des prospects, avec filtres par statut et par projet et une recherche ;
- les messages prévus pour chaque prospect (bouton « Messages ») ;
- des boutons **Valider** (pour un prospect « à vérifier »), **STOP**, **Pause d'urgence**, **Reprendre** et **Lancer un passage** ;
- l'import d'un fichier CSV et l'activité récente.

La page se rafraîchit toute seule chaque minute. Pour arrêter le tableau de bord, faites Ctrl + C dans le Terminal.

## Au quotidien (en ligne de commande)

| Besoin | Commande |
|---|---|
| Voir l'état et les quotas | `python -m prospectia_outreach status` |
| Voir les réponses à traiter | `python -m prospectia_outreach replies` |
| Ajouter quelqu'un en liste STOP | `python -m prospectia_outreach optout <url LinkedIn ou email>` |
| Tout arrêter immédiatement | `python -m prospectia_outreach pause "raison"` |
| Reprendre | `python -m prospectia_outreach resume` |

**Répondre aux personnes intéressées reste votre travail.** Le moteur s'arrête dès qu'il y a une réponse.

## Garde-fous intégrés

- **Montée en charge** : 5 invitations le premier jour, puis +2 par jour d'activité, jusqu'à 15 par jour et 80 par semaine.
- **Messages** : 30 par jour au maximum.
- **Pause automatique** : si LinkedIn ou Unipile signale une limite, ou si le compte est déconnecté ou demande une vérification, **tout se met en pause**. Vérifiez le compte LinkedIn à la main avant de taper `resume`.
- **Invitations expirées** : une invitation non acceptée après 21 jours est abandonnée.
- **Erreurs** : une erreur sur un prospect le marque `error` sans bloquer les autres.

Tous ces chiffres se règlent dans `.env`. Ce sont des réglages de prudence, pas des limites officielles de LinkedIn : **ne les augmentez pas les premières semaines.**

## RGPD : ce que le module fait et ce qui reste à faire

**Ce que le module fait :**
- Il cite la source de l'information et propose « STOP » dans le premier message.
- Il tient une liste « ne plus contacter » permanente, qui survit même si la fiche est supprimée.
- Il ne recontacte jamais deux fois la même personne.
- Il garde un historique de chaque action (table `events`).

**Ce qui reste à faire par l'association :**
- Signer le contrat de sous-traitance des données (DPA) avec Unipile.
- Inscrire ce traitement au registre RGPD.
- Publier une politique de confidentialité.
- Ne jamais importer de coordonnées que les personnes ont masquées sur LinkedIn.
- Supprimer les prospects sans réponse après la durée choisie (12 mois suggérés).

## Si Unipile change son API

Tous les appels réseau sont dans `prospectia_outreach/unipile_client.py`. Ce fichier gère la v2 d'Unipile (par défaut) et l'ancienne v1 (`UNIPILE_VERSION=v1` avec `UNIPILE_DSN`). La v2 est en bêta chez Unipile : si un appel change, c'est le seul fichier à corriger.
