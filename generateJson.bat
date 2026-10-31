tar -a -c -f "CableGuy.zip" "__init__.py" "blender_manifest.toml"
blender --command extension server-generate --repo-dir "C:\Users\breum/OneDrive/Documenten/Code/CableGuy"