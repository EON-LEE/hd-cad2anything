# hd-cad2anything

## GitHub authentication

Keep `eonlee_MSDEMO` as the default GitHub CLI account. For `EON-LEE/*`
repositories, use the existing EON-LEE Git credentials for fetch and push; do
not add `~/.gitconfig-eonlee-auth` again with `git -c include.path=...`. For
GitHub CLI write operations, scope the EON-LEE token to the individual command
by obtaining it with `gh auth token --user EON-LEE`, and remove `GH_TOKEN`
immediately afterward.