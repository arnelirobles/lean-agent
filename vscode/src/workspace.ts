import { execFile } from "node:child_process";
import * as path from "node:path";

/** Names that may match items.project for a folder: its own name and the git main repo name. */
export function projectNames(folder: string): Promise<string[]> {
  const names = [path.basename(folder)];
  return new Promise((resolve) => {
    execFile("git", ["rev-parse", "--path-format=absolute", "--git-common-dir"], { cwd: folder, timeout: 5000 }, (err, out) => {
      if (!err) {
        const common = String(out).trim();
        if (common) names.push(path.basename(common.endsWith(".git") ? path.dirname(common) : common));
      }
      resolve([...new Set(names.map((n) => n.toLowerCase()))]);
    });
  });
}
