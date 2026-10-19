import * as fs from "node:fs";
import initSqlJs, { Database, SqlJsStatic } from "sql.js";

/** Loads the brain file into memory on each refresh. Reading bytes means the file is never opened for writing. */
export class Store {
  private sql?: SqlJsStatic;
  private db?: Database;
  lastGood = 0;
  note = "";

  constructor(private readonly wasmPath: string) {}

  async init(): Promise<void> {
    this.sql = await initSqlJs({ wasmBinary: fs.readFileSync(this.wasmPath) as unknown as ArrayBuffer });
  }

  get database(): Database | undefined {
    return this.db;
  }

  private tryLoad(file: string): Database {
    const bytes = fs.readFileSync(file);
    const db = new this.sql!.Database(new Uint8Array(bytes));
    db.exec("SELECT value FROM meta WHERE key='schema_version'"); // fails if the file is mid-write or not a brain
    return db;
  }

  /** Fresh read. One retry after a short pause, then keep the last good data and set a note. */
  async refresh(file: string): Promise<boolean> {
    if (!this.sql) await this.init();
    if (!fs.existsSync(file)) {
      this.note = `No brain database at ${file}.`;
      return false;
    }
    let lastErr: unknown;
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        const next = this.tryLoad(file);
        this.db?.close();
        this.db = next;
        this.lastGood = Date.now();
        this.note = "";
        return true;
      } catch (e) {
        lastErr = e;
        if (attempt === 0) await new Promise((r) => setTimeout(r, 400));
      }
    }
    const msg = lastErr instanceof Error ? lastErr.message : String(lastErr);
    this.note = this.db
      ? `The database could not be read (${msg}). Showing the last good data from ${new Date(this.lastGood).toLocaleTimeString("en-GB")}.`
      : `The database could not be read (${msg}).`;
    return false;
  }
}
