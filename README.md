## Digital Marketplace Runner
A utility script that will run your API/frontend repositories together and allow you to minimally interact with the
running processes and their logs. This script is primarily compatible with OSX; running against other OSs may require
some care and consideration. At its simplest, as an existing developer, you should be able to clone this repo and
simply type `make` to run all your checked out apps.

## Prerequisites / Assumptions
* You have a pre-existing development environment, including eg:
  * Python 3 (with headers) installed globally with `pip` and `virtualenv` packages.
  * Backend services installed and running (as of writing: postgresql-9.5, elasticsearch-5.4.2, nginx, nodejs)
  * Other dependencies with headers as appropriate (openssl, libssl-dev, libxml2-dev, libxslt-dev, libffi-dev,
    libyaml-dev, lib32ncurses-dev, postgresql-server-dev). Others may be required.
  * Postgres populated with development data from Google Drive.
  * Nginx bootstrapped with the required routing (eg from `digitalmarketplace-functional-tests/nginx/bootstrap.sh`).
  * Nginx config installed at `/usr/local/etc/nginx/nginx.conf` (default location) **or** you have digitalmarketplace-functional-tests checked out alongside app repos.
  * Your api/frontend repos should have relatively up-to-date code, containing Python 3 virtualenvs.
  * Your api/frontend repos are checked out next to each other on your filesystem.
  * You hav Nix installed (only required if you want to use it the runner's Nix option).

## Instructions
### Fresh start
1. Clone this repository into an empty directory (e.g. `~/gds`, `~/dm`, or whatever), so that you have something like 
`~/gds/dmrunner`.
2. Run `make download`.
3. Ensure all required dependencies are in place (including dev data in postgres).
4. Run `make all`.

### Existing repos
1. Clone this repository to the same directory that contains your api/frontend repos.
2. `cd` into the `dmrunner` repository.
3. `make` to launch all digitalmarketplace apps that you have checked out. If any of your repos are newly checked out,
   or may have missing/out-of-date virtualenvs, use `make all`.
4. Once the initial boot-up sequence is complete, you'll see a prompt. A number of commands are available to interact
   with your running processes. You also have command history available with arrow keys and tab completion for app
   names.

## `make` commands
### Options
* `make download` - Clones all api/frontend repositories.
* `make`/`make run` - Launches all repos using `make run-app`.
* `make all` - Launches all repos using `make run-all`.
* `make nix` - Launches all repos using `make run-app` inside the repo's `nix-shell`.
* `make nix-all` - Launches all repos using `make run-all` inside the repo's `nix-shell`.

## Arguments
You can supply a number of optional arguments to `make` with `make ARGS='' <goal>`':
 * `--all` - use `make run-all` against each repository rather than `make run-app`
 * `--nix` - run each app through nix

## Commands
After the apps have done their initial boot-up cycle, you have a few commands at your disposal:

#### H / Help
Print out a list of commands available to you.

#### S / Status
Print out current status of the running apps.

#### B / Branch / Branches
Print which branches you're running for each app.

#### R / Restart
Restart any failed or down apps using `make run-app`.

#### Remake
Restart any failed or down apps using `make run-all`.

#### F / Filter
By default, incoming logs from all apps are interleaved together. You can use this to toggle on a filter to show only
logs from specific apps. With no arguments, this command resets any filters, showing all logs. With arguments, the
closest-matching app name for each word will be filtered in for logging purposes (eg 'f api buyer' will cause only
logs for the data api and the buyer-frontend to be shown).

#### FE / frontend
Runs `make frontend-build` against frontend apps. With arguments, the closest-matching app name for each word will be
rebuilt (eg 'fe buyer supplier' will run a `frontend-build` on the buyer-frontend and the supplier-frontend. With no
arguments, all frontend apps will be rebuilt. This is a one-off rebuild; for ongoing rebuilds use `FW`.

#### K / Kill
Kill apps that are running. With no arguments, all apps will be killed. With arguments, the closest-matching app name
for each word will be killed (eg 'k search briefs' will take down the search-api and the briefs-frontend).

#### Q / Quit
Kill all running apps and quit back to your shell.

## Logs
Regardless of filtering, all logs are also stored in `./logs` for later reading if required.

## Troubleshooting
* If the program crashes, it might leave processes running around behind the scenes. Use `ps -ef|grep "python\|nix"` to find any
orphaned processes and then `kill` them before re-launching the runner.
* If any apps report "make[1]: *** No rule to make target `run-app'.  Stop.", they're really out of date. Update them and run-all/'remake'.

## Todo
* Refactoring...
* Manage config better.
* Use our standard dockerised backend services for elasticsearch/postgres/nginx so that this can be a quick setup for
  new developers.
* Look into using dockerised apps with volumes for local code. (i.e. `make docker`, eventually probably just to replace
  the default `make`))
* Move app colors out to config file
* Upgrade `make download` to `make new` which fully sets-up dev environment from scratch (maybe not viable/desirable).
