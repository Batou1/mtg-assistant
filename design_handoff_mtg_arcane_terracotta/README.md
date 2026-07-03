# Handoff : Refonte UI « Arcane Terracotta » — MTG Assistant

## Overview
Refonte visuelle de l'app **mtg-assistant** (https://github.com/Batou1/mtg-assistant — FastAPI + templates Jinja/HTML + CSS). Quatre écrans redessinés dans une direction unique « Arcane Terracotta » : sombre & premium, tons chauds, accent terracotta, couleurs de mana (WUBRG) en accents secondaires, **avec mode sombre ET mode clair**.

## About the Design Files
Les fichiers de ce dossier sont des **références de design en HTML** (maquettes interactives), pas du code de production. La tâche est de **recréer ces designs dans le codebase existant** (templates Jinja + CSS statique de l'app FastAPI), en suivant ses patterns. Ne pas copier le HTML tel quel.

Ouvrir `MTG Assistant Chat.dc.html` dans un navigateur (avec `support.js` à côté) pour voir les maquettes. Le fichier contient plusieurs itérations empilées ; **la direction finale est** :
- Section « Itération 4 » : écrans **4a Accueil**, **4b Ma collection**, **4c Depuis une liste**
- Section « Itération 2 » : écran **2a Chat** (référence de la direction)
- Les sections « Itération 3 » (nuancier) et « 1a / 1b » (explorations abandonnées) sont à **ignorer**.
Chaque maquette a un bouton clair/sombre en haut à droite — implémenter les deux modes.

## Fidelity
**High-fidelity.** Couleurs, typo, espacements et composants sont définitifs. Reproduire fidèlement, en s'adaptant aux composants existants de l'app.

## Design Tokens

Implémenter en **variables CSS** sur `:root` (sombre par défaut) avec un override `[data-theme="light"]` (ou `.light` sur `<html>`), persisté en cookie/localStorage + respect de `prefers-color-scheme` par défaut. Les valeurs sont en `oklch()` (support natif dans tous les navigateurs modernes).

### Mode sombre (défaut)
| Token | Valeur | Usage |
|---|---|---|
| `--bg` | `oklch(0.155 0.012 60)` | Fond de page |
| `--bg2` | `oklch(0.195 0.014 58)` | Panneaux, cartes, topbar, sidebar |
| `--bg3` | `oklch(0.235 0.016 55)` | Surfaces surélevées : chips, inputs, hovers |
| `--line` | `oklch(0.30 0.018 55)` | Bordures, séparateurs |
| `--text` | `oklch(0.955 0.008 75)` | Texte principal |
| `--muted` | `oklch(0.66 0.014 68)` | Texte secondaire |
| `--accent` | `oklch(0.71 0.125 45)` | Accent terracotta (boutons, actifs, liens) |
| `--accent2` | `oklch(0.63 0.13 32)` | 2e teinte des dégradés d'accent |
| `--accent-ink` | `oklch(0.24 0.04 45)` | Texte posé SUR l'accent |
| `--ring-track` | `oklch(0.30 0.018 55)` | Piste des barres de progression |

### Mode clair
| Token | Valeur |
|---|---|
| `--bg` | `oklch(0.975 0.006 80)` |
| `--bg2` | `oklch(1 0 0)` |
| `--bg3` | `oklch(0.958 0.008 80)` |
| `--line` | `oklch(0.89 0.01 80)` |
| `--text` | `oklch(0.24 0.014 60)` |
| `--muted` | `oklch(0.48 0.014 66)` |
| `--accent` | `oklch(0.55 0.12 42)` |
| `--accent2` | `oklch(0.60 0.125 32)` |
| `--accent-ink` | `oklch(0.99 0.01 50)` |
| `--ring-track` | `oklch(0.89 0.01 80)` |

### Couleurs de mana (identiques dans les 2 modes)
`--mw: oklch(0.90 0.04 90)` · `--mu: oklch(0.66 0.13 245)` · `--mb: oklch(0.55 0.035 300)` · `--mr: oklch(0.64 0.17 25)` · `--mg: oklch(0.62 0.12 150)`

### Typographie (Google Fonts)
- **Titres / boutons principaux** : `Bricolage Grotesque` (700–800, letter-spacing −0.01em)
- **Corps** : `Hanken Grotesk` (400–700)
- **Chiffres / prix / stats** : `JetBrains Mono` (400–600)
- Échelle : h1 hero 38px/800 · h2 page 26–28px/800 · titre de carte 21px/700 · corps 14.5px · secondaire 12.5–13.5px · labels uppercase 11–12px, letter-spacing .1–.12em

### Rayons, ombres, divers
- Boutons & chips : `border-radius: 999px` (pilule — signature de la direction). Panneaux/cartes : 16–18px. Images de cartes : 10–12px. Inputs rectangulaires : 12px.
- Ombre de panneau : `0 20px 50px -22px rgba(0,0,0,.72)` (sombre) / `0 20px 50px -26px rgba(45,32,15,.20)` (clair)
- Dégradé d'accent (boutons primaires, avatar, bulle utilisateur) : `linear-gradient(120deg, var(--accent), var(--accent2))`
- **Barre WUBRG signature** : filet de 2px sous la topbar, `linear-gradient(90deg, --mw, --mu, --mb, --mr, --mg)` à `opacity: 0.5`
- Bordure gauche de 3px sur les cartes de commandant, colorée selon l'identité couleur du commandant (ex : `--mb` pour mono-noir)

## Screens / Views

### 1. Chat (`/chat`) — maquette 2a
- **Topbar 62px** (bg2, bordure basse + filet WUBRG) : logo (carré 30px dégradé accent, glyphe ✦) + « MTG Assistant » ; nav en pilules (item actif : fond accent, texte accent-ink, 700 ; inactifs : muted) ; à droite : toggle thème, pilule profil (avatar 26px dégradé + prénom), bouton réglages rond 36px.
- **Sidebar 290px** (bg2, bordure droite) : bouton pilule pleine largeur 44px « + Nouvelle conversation » (dégradé accent) ; label « CONVERSATIONS » (11px uppercase muted) ; items 11px 12px de padding, radius 11px, 13.5px — actif : fond accent + texte accent-ink 700 ; hover : bg3. En bas, bloc « Propositions » : carte bordée `color-mix(in oklch, var(--accent) 55%, transparent)`, fond accent à 10%.
- **Fil de discussion** (padding 26px 34px, gap 22px) :
  - Pilule de statut centrée « Claude connecté · claude-sonnet-5 » avec point vert (`--mg`) et glow.
  - **Bulle utilisateur** : alignée à droite, max 70%, dégradé accent, texte accent-ink 600, radius `16px 16px 4px 16px`.
  - **Réponse assistant** : avatar rond 32px (bg3, bordure line, glyphe ✦ accent) + colonne de contenu.
  - **Chips d'intention** : Format / Couleur (avec pip de mana 14px) / Thème / Budget — pilules bg3 bordées, 12px.
  - **Carte commandant** (composant clé) : panneau bg2 bordé, radius 18px, padding 20px, bordure gauche 3px couleur d'identité ; **image Scryfall 176×245px** (radius 12px, liseré `color-mix(in oklch, var(--accent) 30%, transparent)` + ombre portée) ; nom 21px Bricolage 700 ; « N decks · EDHREC » en mono muted ; badge « ✓ Possédé » (pilule fond `--mg` à 20%, texte `--mg`) ; **barre de complétude** 9px (piste ring-track, remplissage dégradé accent2→accent, % en Bricolage 700 16px accent, « 93 / 229 cartes possédées ») ; **liste d'achat** : encart bordé radius 13px — en-tête bg3 avec « Liste d'achat · N cartes · total € / budget € » (total en mono accent), corps = chips pilules « Nom carte + prix mono » + chip « +N autres » ; boutons : « Générer la decklist complète » (pilule 44px dégradé accent, flex 1) + « Détails » (pilule outline).
  - Entrée en scène : `fadeUp` 0.5s (opacity 0 → 1, translateY 10px → 0), décalée de 0.1s par carte.
- **Barre de saisie** : bandeau bg2 bordé en haut ; à l'intérieur, pilule bg3 bordée avec input transparent (placeholder : « Écris ton message…  “plutôt en mono-noir”, “monte le budget à 80 €” ») + bouton d'envoi rond 44px dégradé accent avec icône avion en papier (stroke 2.2).

### 2. Accueil (`/`) — maquette 4a
Grille 2 colonnes `1.25fr / 0.75fr`, padding 48px 52px, gap 28px.
- **Gauche** : h1 38px « Quel deck veux-tu construire aujourd'hui ? » + sous-titre muted ; **carte d'envie** (bg2, radius 18px) : zone de texte multi-lignes avec placeholder exemple, ligne basse = statut « Claude connecté » + bouton pilule 46px « Lancer l'analyse » ; 3 chips d'exemples cliquables (remplissent le champ).
- **Droite** : carte « Ma collection » (stats 1 248 cartes / 736 uniques / 842 € en mono 20px + **barre de répartition WUBRG** 8px segmentée + lien « Voir tout → » accent) ; **dropzone ManaBox** (bordure dashed 1.5px, icône ↑ accent, « .csv — stocké en local, par profil ») ; carte « Commandants proposés · 5 options » (même style que la sidebar du chat, flèche → accent).

### 3. Ma collection (`/collection`) — maquette 4b
Padding 30px 40px.
- **En-tête** : h2 26px + stats mono muted + bouton pilule « Importer ManaBox » à droite.
- **Filtres** : champ de recherche pilule 42px (max 340px) ; 5 **pips de mana cliquables** 26px (filtre actif : bordure accent ; inactifs : bordure line) ; chips « Type · Tous », « Tri · Valeur ↓ ».
- **Grille de cartes** : `repeat(6, 1fr)`, gap 16px ; chaque tuile = image Scryfall `aspect-ratio: 0.717`, radius 10px, **badge quantité** superposé haut-droite (`×4`, mono 11px, fond `rgba(0,0,0,.72)`, texte blanc) ; dessous : nom 12px 600 (ellipsis) + prix mono accent aligné à droite. Prévoir pagination/virtualisation au-delà d'une page.

### 4. Depuis une liste (`/build`) — maquette 4c
Grille 2 colonnes `1fr / 0.85fr`, padding 40px 52px.
- **Gauche** : h2 28px « Construis un deck depuis ta liste » + sous-titre ; **zone de collage** (bg2, radius 16px, contenu en JetBrains Mono 13px, line-height 1.9) ; dropzone dashed « ou dépose un fichier .txt / .csv ManaBox ».
- **Droite** (labels uppercase 12px muted 700) : **Format** = chips pilules (Limité 40, Commander actif = fond accent, Modern, Pauper, Premodern, +3) ; **Couleurs (optionnel)** = 5 pips 30px, sélection = bordure accent + halo `0 0 0 3px color-mix(in oklch, var(--accent) 30%, transparent)` ; **Budget bonus** et **Prix max / carte** = 2 inputs mono côte à côte (44px, radius 12px) ; **Thème (optionnel)** = input texte ; CTA pilule 52px « Construire le deck » ; note centrée « Le résultat s'ouvre dans le chat — tu peux itérer en conversation. »

## Interactions & Behavior
- **Toggle thème** : bouton topbar (icône ☾/☀ + libellé « Sombre »/« Clair ») ; défaut = `prefers-color-scheme` ; choix persisté (localStorage ou cookie, cohérent avec le système de profil existant).
- Hovers : items de nav/conversations → bg3 ; boutons primaires → légère élévation ou luminosité +4% ; chips exemples → bordure accent.
- Cartes commandant : apparition `fadeUp` 0.5s, +0.1s de décalage par carte.
- Liste d'achat : l'en-tête est cliquable pour déplier la liste complète (état replié = chips aperçu + « +N autres »).
- « Générer la decklist complète » : conserver le flux existant ; prévoir un état chargement (bouton désactivé + spinner accent).
- Statut LLM : point vert `--mg` si connecté ; si clé absente, point `--muted` + « mode heuristique ».

## State Management
Aucun nouveau state métier — reprendre l'existant (profils, conversations, propositions). Nouveaux states UI : thème (dark/light), buylist dépliée/repliée, filtres collection (recherche, couleurs, tri).

## Assets
- **Images de cartes** : l'app a déjà `scryfall.image()` — utiliser les `image_uris` locales. Les maquettes utilisent `https://api.scryfall.com/cards/named?exact=…&format=image` uniquement comme placeholder.
- **Icônes** : SVG inline stroke (avion d'envoi stroke-width 2.2) ; glyphes ✦ / ⚙ / ↑ / ⌕ remplaçables par des SVG équivalents (Lucide p.ex.).
- **Fonts** : Bricolage Grotesque, Hanken Grotesk, JetBrains Mono via Google Fonts (ou auto-hébergées).

## Files
- `MTG Assistant Chat.dc.html` — maquettes interactives (ouvrir dans un navigateur ; `support.js` requis à côté). Écrans finaux : 2a (Chat), 4a (Accueil), 4b (Collection), 4c (Depuis une liste). Sections 1a/1b/nuancier = explorations, à ignorer.
- `support.js` — runtime des maquettes (ne pas réutiliser en production).
