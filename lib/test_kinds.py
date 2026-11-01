"""What test-phase can run, named once for the queue and the runner."""

DOTNET_FILES = (".csproj", ".fsproj", ".vbproj", ".sln", ".slnx", ".slnf")
SUPPORTED_KINDS = ("a dotnet project or solution (.csproj, .fsproj, .vbproj, .sln, .slnx, .slnf) or a folder holding one, "
                   "a package.json, a playwright.config.ts, .js, .mjs or .cjs, or a Gradle settings file or its folder")
