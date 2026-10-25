use crate::{Error, Host, METHODS, Provider, host::require, json};
use serde_json::{Value, json};
use std::io::{Read, Write};
const MAX_FRAME: usize = 1024 * 1024;
fn read_frame(input: &mut impl Read) -> Result<Option<Value>, Error> {
    let mut header = [0u8; 4];
    loop {
        match input.read(&mut header[..1]) {
            Ok(0) => return Ok(None),
            Ok(_) => break,
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(_) => return Err(Error::failed("cannot read frame")),
        }
    }
    input
        .read_exact(&mut header[1..])
        .map_err(|_| Error::failed("incomplete frame header"))?;
    let size = u32::from_be_bytes(header) as usize;
    if !(2..=MAX_FRAME).contains(&size) {
        return Err(Error::failed("invalid frame size"));
    }
    let mut body = vec![0u8; size];
    input
        .read_exact(&mut body)
        .map_err(|_| Error::failed("incomplete frame body"))?;
    json::parse(&body).map(Some)
}
fn write_frame(output: &mut impl Write, value: Value) -> Result<(), Error> {
    let bytes = serde_json::to_vec(&value).map_err(|_| Error::failed("cannot encode response"))?;
    if bytes.len() > MAX_FRAME {
        return Err(Error::failed("response too large"));
    }
    output
        .write_all(&(bytes.len() as u32).to_be_bytes())
        .and_then(|()| output.write_all(&bytes))
        .and_then(|()| output.flush())
        .map_err(|_| Error::failed("cannot write response"))
}
fn envelope(value: &Value, id: u64) -> Result<(), Error> {
    require(value, &["protocol_version", "id", "method", "params"])?;
    if value["protocol_version"].as_u64() != Some(2)
        || value["id"].as_u64() != Some(id)
        || !value["method"].is_string()
    {
        return Err(Error::failed("invalid request envelope"));
    }
    Ok(())
}
/// Serve exact big-endian length-prefixed Host V2 frames. EOF is clean only between frames.
/// Invalid handshake/envelopes terminate the connection; operation errors are framed.
pub fn serve<P: Provider>(
    host: &mut Host<P>,
    input: &mut impl Read,
    output: &mut impl Write,
) -> Result<(), Error> {
    let hello = read_frame(input)?.ok_or_else(|| Error::failed("missing hello"))?;
    envelope(&hello, 1)?;
    let p = &hello["params"];
    require(
        p,
        &[
            "protocol_versions",
            "host_api",
            "plugin_id",
            "required_capabilities",
        ],
    )?;
    if hello["method"] != "hello"
        || p["protocol_versions"] != json!([2])
        || p["host_api"].as_u64() != Some(1)
        || p["plugin_id"] != "cmux-host"
        || p["required_capabilities"] != json!(METHODS)
    {
        return Err(Error::failed("incompatible hello"));
    }
    write_frame(
        output,
        json!({"protocol_version":2,"id":1,"result":host.handshake()?}),
    )?;
    let mut expected_id = 2u64;
    while let Some(request) = read_frame(input)? {
        envelope(&request, expected_id)?;
        let method = request["method"]
            .as_str()
            .ok_or_else(|| Error::failed("invalid method"))?;
        let response = match host.dispatch(method, &request["params"]) {
            Ok(result) => json!({"protocol_version":2,"id":expected_id,"result":result}),
            Err(error) => {
                json!({"protocol_version":2,"id":expected_id,"error":{"code":error.code,"message":error.message}})
            }
        };
        write_frame(output, response)?;
        expected_id = expected_id
            .checked_add(1)
            .ok_or_else(|| Error::failed("request id exhausted"))?;
    }
    Ok(())
}
