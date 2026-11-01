"""What test-phase can run, named once for the queue and the runner."""
import re

DOTNET_FILES = (".csproj", ".fsproj", ".vbproj", ".sln", ".slnx", ".slnf")
# A Go package pattern: ".", "./..." or "./cmd/x". It starts with a dot, so it is never read as a flag.
GO_PATTERN = re.compile(r"^(?:\.|\./\S*|\.\.\.)$")
SUPPORTED_KINDS = ("a dotnet project or solution (.csproj, .fsproj, .vbproj, .sln, .slnx, .slnf) or a folder holding one, "
                   "a package.json, a playwright.config.ts, .js, .mjs or .cjs, a Gradle settings file or its folder, or a Go package pattern (./... or ./cmd/x) in a repo with go.mod")
