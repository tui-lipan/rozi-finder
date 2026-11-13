# Scene for the extension gallery screenshot; see the rozi-extension skill's screenshots guide.
# Capture with `--config docs/screenshot-config.toml`, which keeps rozi's first-run notice out.
# A staged repository with files of different ages and a few uncommitted changes, two panes, and a
# stand-in `zoxide` for the Jump tab. The picker is open on Files with a filter typed.
repo=$HOME/src/atlas
mkdir -p "$repo"/{src/{api,store,ui},tests,docs,scripts} "$HOME/src/atlas-web" "$HOME/notes"
git init -q -b main "$repo"
stage() { # stage AGE PATH...: files last changed AGE ago
    local age=$1 path
    shift
    for path in "$@"; do
        echo "// $path" > "$repo/$path"
        touch -d "$age ago" "$repo/$path"
    done
}
stage "40 days" README.md Cargo.toml docs/architecture.md docs/deploy.md scripts/release.sh
stage "9 days" src/main.rs src/config.rs src/ui/theme.rs src/ui/layout.rs tests/config.rs
stage "3 days" src/store/mod.rs src/store/migrations.rs src/api/mod.rs tests/store.rs
stage "5 hours" src/api/routes.rs src/api/auth.rs tests/api.rs
git -C "$repo" add -A
git -C "$repo" -c user.name=demo -c user.email=demo@example.invalid commit -q -m init
stage "12 minutes" src/api/session.rs src/store/cache.rs
stage "2 minutes" tests/auth.rs
echo "// verify tokens" >> "$repo/src/api/auth.rs"
touch -d "2 minutes ago" "$repo/src/api/auth.rs"
echo "// session routes" >> "$repo/src/api/routes.rs"
touch -d "5 minutes ago" "$repo/src/api/routes.rs"
git -C "$repo" add src/api/routes.rs

bin zoxide "printf '%s\n' '$HOME/src/atlas-web' '$HOME/notes'"

keys "cd ~/src/atlas" Enter
rozi split --cwd "$HOME/src/atlas-web" --argv bash >/dev/null
rozi focus 1 >/dev/null
settle 300
keys "clear; git status --short" Enter
settle 600
keys "git diff "
rozi run-action finder.files
settle 1200
keys "api"
settle 600
