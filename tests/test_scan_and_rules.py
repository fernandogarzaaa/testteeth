"""The brace-language scanner and the TypeScript / Rust failure-path rule packs (heuristic)."""

from __future__ import annotations

import textwrap

from testteeth.languages.scan import mask, scan
from testteeth.rules import analyze
from testteeth.rules import test_bodies as split_tests


def ts(src: str):
    return scan(textwrap.dedent(src).lstrip("\n"), "typescript")


def rs(src: str):
    return scan(textwrap.dedent(src).lstrip("\n"), "rust")


def kinds(gaps):
    return sorted(g.kind for g in gaps)


# ------------------------------------------------------------------------------------------- scanner
def test_mask_keeps_offsets_and_hides_strings_and_comments():
    src = 'const a = "{ not a brace }"; // { comment\n/* { */ const b = `x ${y}`;\n'
    masked = mask(src, "typescript")
    assert len(masked) == len(src) and masked.count("\n") == src.count("\n")
    assert "{" not in masked and "comment" not in masked and "const b" in masked


def test_mask_rust_lifetimes_chars_raw_strings_and_nested_comments():
    src = "fn f<'a>(x: &'a str) -> char { let c = '{'; let r = r#\"}\"#; /* a /* b */ } */ 'x' }\n"
    masked = mask(src, "rust")
    assert masked.count("{") == 1 and masked.count("}") == 1  # only the fn body braces survive
    assert "&'a str" in masked


def test_ts_functions_methods_arrows_and_classes():
    s = ts(
        """
        export async function load(id: string): Promise<User> {
          return api.get(id);
        }
        const double = (x: number) => x * 2;
        export const handler = async (req: Req) => {
          if (req.ok) {
            return 1;
          }
        };
        class Repo {
          private items = [];
          async save(item: Item): Promise<void> {
            this.items.push(item);
          }
          get size(): number {
            return this.items.length;
          }
        }
        """
    )
    names = {f.qualname: (f.line, f.end_line, f.is_async) for f in s.functions}
    assert names["load"] == (1, 3, True)
    assert names["double"][0] == 4 and names["double"][1] == 4
    assert names["handler"] == (5, 9, True)
    assert names["Repo.save"] == (12, 14, True)
    assert "Repo.size" in names
    assert s.function_at(7).qualname == "handler"
    assert s.function_at(11) is None and s.scope_at(11) == ("Repo", 10)


def test_rust_functions_impls_traits_and_test_modules():
    s = rs(
        """
        pub struct Ledger;
        impl Ledger {
            pub fn record(&mut self) -> Result<(), E> {
                Ok(())
            }
        }
        impl<T: Clone> Display for Wrapper<T> {
            fn fmt(&self, f: &mut Formatter<'_>) -> fmt::Result { Ok(()) }
        }
        pub async fn fetch() {}
        #[cfg(test)]
        mod tests {
            fn helper() {}
            #[test]
            fn records() { helper(); }
        }
        """
    )
    by_name = {f.qualname: f for f in s.functions}
    assert by_name["Ledger::record"].line == 3 and not by_name["Ledger::record"].is_test
    assert "Wrapper::fmt" in by_name
    assert by_name["fetch"].is_async
    assert by_name["tests::helper"].is_test and by_name["tests::records"].is_test
    assert s.function_at(15) is None  # inside a test module: not source
    assert s.outermost_at(4).qualname == "Ledger::record"


# ------------------------------------------------------------------------------------------- TypeScript
TS_SRC = """
export async function chargeWithRetry(gateway: Gateway, attempts = 3) {
  for (let attempt = 0; attempt < attempts; attempt++) {
    try {
      return await gateway.charge("t", { timeoutMs: 5000 });
    } catch (err) {
      if (attempt === attempts - 1) throw err;
    }
  }
}

export function loadUser(id: string) {
  return fetch(`/users/${id}`).then((r) => r.json()).catch(() => null);
}

export async function withDeadline(ms: number) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), ms);
  return axios.get("/x", { signal: controller.signal });
}

export function validate(n: number) {
  if (n < 0) {
    throw new RangeError("negative");
  }
  return n;
}
"""


def test_ts_rules_report_every_unexercised_failure_path():
    s = ts(TS_SRC)
    gaps = analyze(s, "src/pay.ts", ["it('ok', () => { expect(validate(1)).toBe(1) })"])
    by_fn: dict[str, list[str]] = {}
    for g in gaps:
        by_fn.setdefault(g.function, []).append(g.kind)
        assert g.lang == "typescript"
    assert sorted(by_fn["chargeWithRetry"]) == ["catch-branch", "external-call", "retry", "timeout"]
    assert sorted(by_fn["loadUser"]) == ["external-call", "promise-catch"]
    assert sorted(by_fn["withDeadline"]) == ["external-call", "timeout"]
    assert "validate" not in by_fn  # no coverage data: throw lines are only flagged with coverage


def test_ts_rules_use_engine_coverage_for_catch_and_throw():
    s = ts(TS_SRC)
    coverage = {6: False, 23: False, 25: True}  # catch body and throw line never ran
    gaps = analyze(s, "src/pay.ts", [], selected={"chargeWithRetry", "validate"}, coverage=coverage)
    details = {(g.function, g.kind): g.detail for g in gaps}
    assert details[("chargeWithRetry", "catch-branch")] == "`catch` block is never executed by any test"
    assert details[("validate", "throw")] == "`throw RangeError` is never triggered by any test"
    assert all(g.function in {"chargeWithRetry", "validate"} for g in gaps)


def test_ts_failure_tests_silence_the_gaps():
    body = """it("retries", async () => {
      const gateway = { charge: vi.fn().mockRejectedValueOnce(new GatewayTimeout()) };
      await expect(chargeWithRetry(gateway)).rejects.toThrow();
    });"""
    gaps = analyze(ts(TS_SRC), "src/pay.ts", split_tests(body, "typescript"), selected={"chargeWithRetry"})
    assert gaps == []


def test_ts_catch_coverage_counts_as_exercised():
    gaps = analyze(ts(TS_SRC), "src/pay.ts", [], selected={"chargeWithRetry"}, coverage={6: True})
    assert gaps == []


def test_ts_test_bodies_split_per_test_or_fall_back_to_file():
    src = 'describe("x", () => {\n  it("a", () => { f() });\n  test.each([1])("b", () => { g() });\n});\n'
    assert split_tests(src, "typescript") == ['it("a", () => { f() })', 'test.each([1])("b", () => { g() })']
    assert split_tests("const x = 1;", "typescript") == ["const x = 1;"]


def test_plain_settimeout_delay_is_not_a_timeout():
    s = ts("const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));\n")
    assert analyze(s, "a.ts", []) == []


# ------------------------------------------------------------------------------------------- Rust
RS_SRC = """
pub fn parse(input: &str) -> Result<u32, ParseError> {
    if input.is_empty() {
        return Err(ParseError::Empty);
    }
    let n: u32 = input.trim().parse()?;
    Ok(n)
}

pub async fn fetch_price(client: &reqwest::Client) -> Result<f64, Error> {
    let resp = tokio::time::timeout(Duration::from_secs(2), client.get(URL).send()).await;
    match resp {
        Ok(Ok(r)) => Ok(r.json().await.unwrap()),
        Ok(Err(e)) => Err(Error::Http(e)),
        Err(_) => Err(Error::Timeout),
    }
}

pub fn load_config(path: &str) -> Config {
    let text = std::fs::read_to_string(path).expect("config");
    toml::from_str(&text).unwrap()
}

pub fn charge_with_retry(gateway: &mut impl Gateway, attempts: u32) -> Result<String, PayError> {
    for attempt in 0..attempts {
        if let Ok(id) = gateway.charge() { return Ok(id); }
    }
    Err(PayError::GaveUp)
}
"""


def test_rust_rules_report_every_unexercised_failure_path():
    gaps = analyze(rs(RS_SRC), "src/lib.rs", [])
    by_fn: dict[str, list[str]] = {}
    for g in gaps:
        by_fn.setdefault(g.function, []).append(g.kind)
        assert g.lang == "rust"
    assert sorted(by_fn["parse"]) == ["error-propagation", "error-return"]
    assert {"match-err", "timeout", "external-call", "unwrap"} <= set(by_fn["fetch_price"])
    assert {"unwrap", "external-call"} <= set(by_fn["load_config"])
    assert {"retry", "error-return", "external-call"} <= set(by_fn["charge_with_retry"])


def test_rust_failure_tests_silence_the_gaps():
    tests = """
    #[test]
    fn rejects_empty() {
        assert_eq!(parse(""), Err(ParseError::Empty));
    }
    #[tokio::test]
    async fn times_out() {
        assert!(matches!(fetch_price(&slow()).await, Err(Error::Timeout)));
    }
    """
    bodies = split_tests(textwrap.dedent(tests), "rust")
    assert len(bodies) == 2
    gaps = analyze(rs(RS_SRC), "src/lib.rs", bodies, selected={"parse", "fetch_price"})
    assert gaps == []


def test_rust_selected_functions_only_and_test_modules_ignored():
    src = RS_SRC + "\n#[cfg(test)]\nmod tests {\n    #[test]\n    fn t() { parse(\"1\").unwrap(); }\n}\n"
    gaps = analyze(rs(src), "src/lib.rs", [], selected={"load_config"})
    assert {g.function for g in gaps} == {"load_config"}
