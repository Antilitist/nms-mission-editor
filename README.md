# NMS Mission Editor

Version 1.0.6.

Edit No Man's Sky mission progress on a **copy** of your save.
Then put that copy back into the game when you are ready.

Sam (Coder), Avea (Art) & Antilitist (Tester, Coder, Modder)

Atlas & Nyx (AI helpers)

## Before you start

- Back up your save first. The tool also makes a zip. Keep your own copy too.
- Close No Man's Sky before you write.
- If Steam Cloud is on, it can put an old file back. Turn Cloud off for No Man's Sky while you test.
- Tested on No Man's Sky **7.05** (build **25625620**).
- Use at your own risk.
- This zip does not include game files. Click **Read my game files** and the tool reads names from your own install.

The window shows the same warning at startup.
It also shows the build it detected.
If that build is not 25625620, the warning says so.

## What it does

- Shows each quest line and its steps.
- Can finish a mission, finish a whole line, or unlock a mission so you can still play it.
- Can reset a mission or a whole line.
- Finish also applies the unlock the game would have applied.
- For In Stellar Multitudes, that unlock turns on purple stars.
- Does not raise ship or frigate counts.
- Does not install technology into a slot.

## Install

You need **Python 3.10 or newer**, 64-bit, from [python.org](https://www.python.org/downloads/).

In the installer, tick:

- Add python.exe to PATH
- Tcl/Tk (the window needs it)

Then:

1. Unzip this folder.
2. Double-click **Start.bat**.
3. The first run creates a `.venv` folder and installs the packages. Later runs skip that.
4. The window opens. There is no second bat file.

If Start.bat says Python is missing, install 3.10 or newer and run it again.

## Pick your save

Start looks for your save folder under `%APPDATA%\HelloGames\NMS`.
It also looks for the game install in Steam.

- If it finds them, you get a slot list.
- If it does not, a folder picker opens.
- For saves, pick the `HelloGames\NMS` folder, or the `st_` folder inside it.
- For the game, pick the folder that contains `GAMEDATA`.

The tool copies the slot into a working copy.
Edits stay in that copy until you put them into the game.

## Read my game files

The first time the tool finds your game, it asks to read your files.
You can also click **Read my game files** later.

That step copies the mission tables, the English language files, and the reward, product, tech, and substance tables out of your PCBANKS folder into a cache on this PC.
It does not change the game.
A new read replaces the previous one.
Raw game files and the extra US-English copies are not kept.
**Clear cache** deletes that copy. The game is not changed.
A bar of text shows how far the read has got. You can keep using the window.

The pak reader is [HGPAKtool](https://github.com/monkeyman192/HGPAKtool) by monkeyman192, MIT license.
Start.bat installs it with the other packages.
It is not copied into this zip.

If those files are still in the game's binary form, the tool downloads [MBINCompiler](https://github.com/monkeyman192/MBINCompiler) (LGPL-3.0) into the cache and runs it.
That program is not in the zip.
The first read can take several minutes on a full install. Later runs reuse the cache.

You can finish a mission from the built-in completion list before that read.
Items and money are granted after the read, when the reward can be resolved.
If a reward still cannot be resolved, the preview says it will be skipped.
If a step has no built-in number, its Finish button stays off and the tip says **Click Read my game files first.**

Friendly names come from every English language file after the read.
A type name such as a class name is not a mission name.
A required chain step with no log title stays `Chain name - step N`.
A comms title from the mission's Dialog table can follow that label.
That title is Dialog, then GcAlienPuzzleEntry, then Title. UI_ROBOMISS_0_COMMS_TITLE is "STARSHIP ALERT".
A step with no such key, such as ^PURPM_BOAT, stays the step label on its own.
The message on a stage is often an event id, so it is not the name.
Missions that are not in a chain use the tidied id.
The status line counts the rows in this list, not every mission in the game files.
Clear cache rebuilds that count from the rows on screen.

## Finish, unlock, or reset

Select a quest line or a step.

- **Finish this mission** marks that step done and applies its unlock.
- **Finish whole quest line** marks the line done, in order, and applies its unlocks.
- **Finish up to this step** marks this step and the ones before it.
- **Unlock only** / **Unlock next step** makes the mission available. It stays unfinished, so you can play it.
- **Reset this mission** sets that step back to not started.
- **Reset whole quest line** sets the line back to not started. Rewards already granted stay.
- **Give items and money** is on by default. It applies when you finish. It does not apply to Unlock only.

Each action asks first, then shows a preview.
Nothing is written until you press **Write to copy**.

## How to turn on purple stars

1. Double-click **Start.bat**.
2. Pick your save slot.
3. Click **In Stellar Multitudes**.
4. Click **Finish whole quest line**.
5. Read the preview. It should say purple systems discovered.
6. Click **Write to copy**.
7. Close the game if it is open.
8. Click **Put edited save into my game**.
9. Start the game. Purple stars show on the galaxy map.

The Atlantid drive is added to known technology when it is missing.
It is not installed into a slot.

If the line already says 5/5 but purple stars are still off, Finish whole quest line stays available.
The preview then lists only the missing unlock. The steps stay as they are.

## Restore a backup

1. Click **Restore a backup**.
2. Pick the zip. The name has the date, the time, and the slot.
3. Confirm.
4. The working copy is put back.
5. Click **Put edited save into my game** if you want the game to use that copy.

**Undo last put-into-game** puts the game slot back to how it was just before the last put.

## If the game updated

This tool was checked on No Man's Sky 7.05, build 25625620.

After a game update:

- The startup box names the new build when it can read it.
- A different build can change mission data. Read the warning before you write.
- Finish values and mission names come from your install, not from this zip.
- If names look wrong after a game update, click **Read my game files** again.
- This zip never includes those extracted files.

## FAQ

- **Will this edit the game while it is open?** Put and Write refuse when the game is running.
- **Where is the backup?** The preview names the zip. It sits in the cache folder for this tool.
- **Can I play the mission instead of skipping it?** Use Unlock only.
- **Does this add ships or frigates?** No.
- **Does the zip include Hello Games files?** No.

## Tips

X Money: Antilitist  |  Cash App: $Antilitist (https://cash.app/$Antilitist)

Handles only.

## About

Open **About / Tip me** in the window for the version, the credits, and the tip line.

Screenshots to add later are listed in `screenshots/README.txt`.
