"""
FastAPI backend for the MiniLang compiler explorer.

Run:   python server.py          (then open http://127.0.0.1:8000)
Docs:  http://127.0.0.1:8000/docs (interactive Swagger UI for the API)
"""
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import minilang

ROOT = Path(__file__).resolve().parent
SAMPLES_DIR = ROOT / "samples"
INDEX_HTML = ROOT / "static" / "index.html"

app = FastAPI(
    title="MiniLang Compiler API",
    description="Lexer, parser, AST, diagnostics and interpreter for the MiniLang language.",
    version="1.0.0",
)

# Lets the UI also work when index.html is opened directly from disk (file://).
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class CompileRequest(BaseModel):
    source: str = Field(..., max_length=200_000, description="MiniLang source code")
    filename: str = Field("<editor>", max_length=200)
    run: bool = Field(True, description="Execute the program if it compiles without errors")


def sample_description(source: str) -> str:
    first = source.lstrip().split("\n", 1)[0]
    if first.startswith("//"):
        return first[2:].strip().split(" - ", 1)[-1]
    return ""


@app.post("/api/compile")
def compile_code(req: CompileRequest) -> dict:
    """Run the source through lexer -> parser -> semantic analysis (-> interpreter)."""
    return minilang.compile_source(req.source, req.filename, run=req.run)


@app.get("/api/samples")
def list_samples() -> list[dict]:
    samples = []
    for path in sorted(SAMPLES_DIR.glob("*.ml")):
        source = path.read_text(encoding="utf-8")
        samples.append({"name": path.name, "description": sample_description(source), "source": source})
    return samples


@app.get("/api/samples/{name}")
def get_sample(name: str) -> dict:
    path = (SAMPLES_DIR / name).resolve()
    if path.parent != SAMPLES_DIR or path.suffix != ".ml" or not path.is_file():
        raise HTTPException(404, f"sample '{name}' not found")
    source = path.read_text(encoding="utf-8")
    return {"name": path.name, "description": sample_description(source), "source": source}


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(INDEX_HTML)


if __name__ == "__main__":
    import uvicorn

    print("MiniLang explorer running at http://127.0.0.1:8000  (Ctrl+C to stop)")
    uvicorn.run(app, host="127.0.0.1", port=8000)
