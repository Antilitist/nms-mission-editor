# Changelog

## 1.0.6

- A comms title such as "STARSHIP ALERT" is read from the mission Dialog table: Dialog, then GcAlienPuzzleEntry, then Title. The first non-empty Title that the language cache can resolve is shown after the step label. A stage Title is not used. ^PURPM_BOAT has no dialog title, so that step stays "In Stellar Multitudes - step N".
- After Write to copy, the selection refresh uses the snapshot the worker already loaded. The window thread does not load or parse the save.

## 1.0.5

- A stage title from the game files, such as "STARSHIP ALERT", follows the step label. The stage message is an event id, so it is not used as the name.
- Clear cache rebuilds the name line from the rows in the list.
- After Write to copy, the save is re-read off the window thread and the list is swapped in.
- Heavy name and table parsing runs in other processes, or yields, so the window is not stuck in long gaps during a read.

## 1.0.4

- A required chain step with no log title stays "Chain name - step N". A notify line from the game files can follow that label. The tidied id is only for missions that are not in a chain.
- The status line counts the rows in this list, not every mission in the game files.
- After a read, the name map and the rows are built off the window thread. The window only applies the finished list.
- The release check also catches phone numbers written with dashes, dots, brackets, or spaces.

## 1.0.3

- The release check no longer stores personal word hashes. A personal list is read only from a local file you do not commit. Without that file, the check still rejects saves, backups, window settings, drive paths, home paths, Steam64 ids, and email addresses, and it says the personal list was skipped.
- A class name such as GcNumberedTextList is not a mission name. Those rows show the tidied id, and the status line counts the same missions the list shows.
- A large name refresh updates the list in chunks, so the window stays usable. Clear cache leaves "Cache cleared." on the status line.

## 1.0.2

- Finish grants items and money from the reward, product, tech, and substance tables copied by Read my game files. A reward that still cannot be resolved says it will be skipped.
- The read keeps only the converted files it needs, replaces the previous read, and Clear cache deletes that copy.
- Names are read from every English language file, including nested text and mission descriptions. The status line counts missions that still have no name.
- The program that turns game files into text is pinned to one version and a checksum. A mismatch is refused.
- The older-files warning stays off when the copied files still match this install.
- The read runs in its own process so the window does not freeze.
- Release files use normal permissions. Personal tokens are checked as hashes.

## 1.0.1

- Read my game files pulls mission tables and English language files from your PCBANKS into a local cache. HGPAKtool (MIT) is installed on first run. MBINCompiler (LGPL-3.0) is downloaded into the cache only when the files are still binary.
- Finish works from the built-in completion list before that read. Items and money wait. A step with no built-in number stays off until you click Read my game files.
- Finish always applies a missing unlock, including In Stellar Multitudes when the steps are already done.
- Friendly names show after the read.
- The first run still opens a save if the key-list download fails.
- Reset names only the steps it changes.

## 1.0.0

- One Start file. The first run creates a Python 3.10 venv in this folder.
- Startup warning: back up, close the game, tested on No Man's Sky 7.05 (build 25625620), use at your own risk.
- The window shows the detected game build and warns when it is not that build.
- Finds the game install and the save folder. A picker opens when either is missing.
- Release zip is built from an allowlist. No saves, backups, or extracted game files.
- Credits: Sam (Coder), Avea (Art) & Antilitist (Tester, Coder, Modder). Atlas & Nyx (AI helpers).

## 0.5.8

- Groups start collapsed. Expand all and Collapse all sit next to Find. Search opens matches.
- A helper mission groups under a quest line only when that line's id prefix is its own.
- Finish whole quest line can apply missing unlocks on a line that is already complete, without moving optional steps.

## 0.5.7

- Story quest lines are top-level rows. Friendly Name says which line they need.
- Optional helper missions sit under Extra steps.

## 0.5.6

- Finish applies the same unlock the game would, including purple systems discovered.

## 0.5.5

- The status line shows the latest check for whether the game is running.

## 0.5.4

- Tests keep their windows and backups off the real cache.

## 0.5.3

- Put, Write, and Restore check that the game is closed at the moment of the write.

## 0.5.2

- The window sizes itself. Large mission tables parse off the window thread.

## 0.5.1

- A warm name cache and table cache paint with the first list.

## 0.5.0

- Warn when the other file of a slot is newer. Mission tables are cached.

## 0.4.9

- A finished quest line stays finished in the buttons. Undo reloads the list.

## 0.4.8

- The open save stays in memory so selecting a row stays quick.

## 0.4.7

- The running-game check does not open a console. A quest line can be finished or reset as a whole.

## 0.4.6

- A same-name copy is backed up before the game file replaces it.

## 0.4.5

- The working copy keeps one live save for the slot.

## 0.4.4

- Window, slot undo, and restore fixes.

## 0.4.3

- Unlock only can unlock the selected mission without finishing it.

## 0.4.2

- Undo can put both files of a slot back.

## 0.4.1

- Undo last put-into-game, including after the window was closed.

## 0.4.0

- Save slot picker and a working copy. The game folder is not edited until you put the save back.

## 0.3.3

- Multi-item rewards, and plainer preview wording.

## 0.3.2

- Reward amounts, currency, and stage versions follow the real mission layout.

## 0.3.1

- Reward and install checks against real saves.

## 0.3.0

- Finish, reset, and the purple-star edit. Dry run unless you write. A backup zip comes first.

## 0.2

- Friendly names, wiki links, and shared titles numbered inside their own run.

## 0.1

- Read-only quest lines: Mission ID, Friendly Name, Status, and Progress.
