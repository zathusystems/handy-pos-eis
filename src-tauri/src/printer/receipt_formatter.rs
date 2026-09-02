// Shared receipt formatter used by desktop and Android print paths.
// Keep this module platform-neutral so both targets produce matching ESC/POS output.

pub const RECEIPT_LINE_WIDTH_30MM: usize = 16;
pub const RECEIPT_LINE_WIDTH_40MM: usize = 21;
pub const RECEIPT_LINE_WIDTH_50MM: usize = 25;
pub const RECEIPT_LINE_WIDTH_58MM: usize = 32;
pub const RECEIPT_LINE_WIDTH_80MM: usize = 42;
pub const DEFAULT_RECEIPT_LINE_WIDTH: usize = RECEIPT_LINE_WIDTH_80MM;
pub const COMPACT_RECEIPT_LINE_WIDTH: usize = RECEIPT_LINE_WIDTH_58MM;

pub fn resolve_line_width(paper_size: Option<&str>) -> usize {
    match paper_size.map(|value| value.trim().to_ascii_lowercase()) {
        Some(value) if value == "30mm" || value == "30" => RECEIPT_LINE_WIDTH_30MM,
        Some(value) if value == "40mm" || value == "40" => RECEIPT_LINE_WIDTH_40MM,
        Some(value) if value == "50mm" || value == "50" => RECEIPT_LINE_WIDTH_50MM,
        Some(value) if value == "58mm" || value == "58" => COMPACT_RECEIPT_LINE_WIDTH,
        _ => DEFAULT_RECEIPT_LINE_WIDTH,
    }
}

pub fn build_escpos_receipt(
    html: &str,
    paper_size: Option<&str>,
    printer_paper_width: Option<&str>,
) -> Vec<u8> {
    let (line_width, horizontal_offset) = resolve_receipt_layout(paper_size, printer_paper_width);
    html_to_escpos(html, line_width, horizontal_offset)
}

pub fn resolve_receipt_layout(
    paper_size: Option<&str>,
    _printer_paper_width: Option<&str>,
) -> (usize, usize) {
    let line_width = resolve_line_width(paper_size);

    (line_width, 0)
}

pub fn cash_drawer_pulse() -> Vec<u8> {
    b"\x1B@\x1Bp\x00\x19\xFA".to_vec()
}

// HTML -> ESC/POS conversion with basic layout preservation.
pub fn html_to_escpos(html: &str, line_width: usize, horizontal_offset: usize) -> Vec<u8> {
    let mut data = Vec::new();
    data.extend_from_slice(b"\x1B\x40"); // Initialize printer
    data.extend_from_slice(b"\x1B\x74\x00"); // Code page
    append_base_text_mode(&mut data, line_width);
    data.extend_from_slice(b"\x1B\x32"); // Restore default line spacing

    let thermal_text = extract_thermal_receipt_text(html);
    let uses_explicit_thermal_layout = thermal_text.is_some();
    let printable_text = thermal_text.unwrap_or_else(|| html_to_printable_text(html, line_width));
    let mut emphasized_company_name = false;
    let mut allow_company_name_detection = !uses_explicit_thermal_layout;
    let qr_payload = extract_qr_payload(html);
    let mut has_qr = false;
    let mut bold_next_company_line = false;

    for line in printable_text.lines() {
        let trimmed = line.trim();
        let lower = trimmed.to_ascii_lowercase();

        if allow_company_name_detection
            && (lower.starts_with("order #:")
                || lower.starts_with("date:")
                || lower.starts_with("cashier:")
                || lower.starts_with("payment:")
                || lower.starts_with("fiscal invoice:"))
        {
            allow_company_name_detection = false;
        }

        if allow_company_name_detection
            && !emphasized_company_name
            && is_company_name_candidate(trimmed)
        {
            append_company_name_banner(&mut data, trimmed, line_width);
            emphasized_company_name = true;
            continue;
        }

        let printable_line = if horizontal_offset > 0 && !line.trim().is_empty() {
            format!("{}{}", " ".repeat(horizontal_offset), line)
        } else {
            line.to_string()
        };

        let bold_current_line = uses_explicit_thermal_layout
            && !trimmed.is_empty()
            && (is_legal_receipt_marker(trimmed)
                || is_copy_marker_line(trimmed)
                || is_vat_registration_marker(trimmed)
                || is_total_line(trimmed)
                || bold_next_company_line);

        if bold_current_line {
            append_bold_mode(&mut data, line_width, true);
        }
        data.extend_from_slice(printable_line.as_bytes());
        if bold_current_line {
            append_bold_mode(&mut data, line_width, false);
        }
        data.extend_from_slice(b"\n");

        if uses_explicit_thermal_layout && !trimmed.is_empty() {
            if bold_next_company_line && !is_legal_receipt_marker(trimmed) {
                bold_next_company_line = false;
            }
            if lower.starts_with("*** start of legal receipt")
                || lower.starts_with("*** start of receipt")
            {
                bold_next_company_line = true;
            }
        }

        if !has_qr && lower.starts_with("scan here for receipt details") {
            if let Some(payload) = qr_payload.as_deref() {
                append_qr_code(&mut data, payload, horizontal_offset, line_width);
                has_qr = true;
            }
        }
    }

    if !has_qr {
        if let Some(payload) = qr_payload.as_deref() {
            append_qr_code(&mut data, payload, horizontal_offset, line_width);
            has_qr = true;
        }
    }

    append_feed_and_cut(&mut data, has_qr);
    data
}

fn append_base_text_mode(data: &mut Vec<u8>, line_width: usize) {
    if line_width < COMPACT_RECEIPT_LINE_WIDTH {
        data.extend_from_slice(b"\x1B\x4D\x01"); // Font B, smaller on most ESC/POS printers
        data.extend_from_slice(b"\x1B\x21\x01"); // Font B through ESC ! for compatible clones
    } else {
        data.extend_from_slice(b"\x1B\x4D\x00"); // Font A
        data.extend_from_slice(b"\x1B\x21\x00"); // Normal mode
    }
}

fn append_bold_mode(data: &mut Vec<u8>, line_width: usize, enabled: bool) {
    if enabled {
        data.extend_from_slice(b"\x1B\x45\x01"); // Emphasized on
        data.extend_from_slice(b"\x1B\x47\x01"); // Double-strike on for printers with weak bold
        if line_width < COMPACT_RECEIPT_LINE_WIDTH {
            data.extend_from_slice(b"\x1B\x21\x09"); // Font B + emphasized
        } else {
            data.extend_from_slice(b"\x1B\x21\x08"); // Font A + emphasized
        }
    } else {
        data.extend_from_slice(b"\x1B\x45\x00"); // Emphasized off
        data.extend_from_slice(b"\x1B\x47\x00"); // Double-strike off
        append_base_text_mode(data, line_width);
    }
}

fn is_receipt_section_title(line: &str) -> bool {
    matches!(
        line.trim().to_ascii_lowercase().as_str(),
        "company info"
            | "order info"
            | "eis compliance"
            | "item breakdown"
            | "tax breakdown"
            | "payment totals"
            | "footer"
    )
}

fn is_divider_line(line: &str) -> bool {
    let trimmed = line.trim();
    !trimmed.is_empty() && trimmed.chars().all(|c| matches!(c, '-' | '=' | '*' | '.'))
}

fn is_dotted_rule(line: &str) -> bool {
    let trimmed = line.trim();
    !trimmed.is_empty()
        && trimmed.chars().count() >= 8
        && trimmed.chars().all(|c| c == '.' || c == '-')
}

fn is_section_heading(line: &str) -> bool {
    matches!(
        line.trim().to_ascii_lowercase().as_str(),
        "invoice" | "buyer details" | "items" | "total amount" | "tax summary"
    )
}

fn is_copy_marker_line(line: &str) -> bool {
    let normalized = line.trim().trim_matches('*').trim().to_ascii_lowercase();

    normalized == "copy"
        || normalized.starts_with("copy #")
        || normalized == "original"
        || normalized.starts_with("original #")
}

fn is_company_name_candidate(line: &str) -> bool {
    let trimmed = line.trim();
    !trimmed.is_empty()
        && !is_divider_line(trimmed)
        && !is_receipt_section_title(trimmed)
        && !is_copy_marker_line(trimmed)
        && !is_legal_receipt_marker(trimmed)
        && !trimmed.contains(':')
}

fn is_legal_receipt_marker(line: &str) -> bool {
    let lower = line.trim().to_ascii_lowercase();
    lower.starts_with("*** start of legal receipt")
        || lower.starts_with("*** end of legal receipt")
        || lower.starts_with("*** start of receipt")
        || lower.starts_with("*** end of receipt")
}

fn is_vat_registration_marker(line: &str) -> bool {
    let normalized = line.trim().trim_matches('*').trim().to_ascii_lowercase();

    normalized == "vat registered" || normalized == "non vat registered"
}

fn is_total_line(line: &str) -> bool {
    line.trim().to_ascii_lowercase().starts_with("total:")
}

fn truncate_with_suffix(value: &str, max_chars: usize) -> String {
    let chars: Vec<char> = value.chars().collect();
    if chars.len() <= max_chars {
        return value.to_string();
    }

    if max_chars <= 3 {
        return ".".repeat(max_chars);
    }

    let mut out: String = chars.into_iter().take(max_chars - 3).collect();
    out.push_str("...");
    out
}

fn append_company_name_banner(data: &mut Vec<u8>, name: &str, line_width: usize) {
    let clean_name = collapse_spaces_preserve_tabs(name);
    if clean_name.is_empty() {
        return;
    }

    let name_len = clean_name.chars().count();

    // Double width+height for shorter names, double height for longer names.
    let (display_name, size_mode): (String, u8) = if name_len <= 20 {
        (clean_name, 0x11)
    } else {
        (truncate_with_suffix(&clean_name, line_width), 0x01)
    };

    data.extend_from_slice(b"\x1B\x61\x01"); // center align
    data.extend_from_slice(b"\x1B\x45\x01"); // bold on
    data.extend_from_slice(&[0x1D, 0x21, size_mode]); // text size
    data.extend_from_slice(display_name.as_bytes());
    data.extend_from_slice(b"\n");
    data.extend_from_slice(b"\x1D\x21\x00"); // normal size
    data.extend_from_slice(b"\x1B\x45\x00"); // bold off
    data.extend_from_slice(b"\x1B\x61\x00"); // left align
}

fn append_feed_and_cut(data: &mut Vec<u8>, has_qr: bool) {
    // Feed enough paper for the legal footer to clear the cutter. Some thermal
    // printers cut very close to the last rendered line, especially after QR.
    let feed_lines: u8 = if has_qr { 7 } else { 5 };
    data.extend_from_slice(&[0x1B, 0x64, feed_lines]); // Print buffer and feed n lines
    data.extend_from_slice(b"\x1D\x56\x00"); // Full cut
}

fn html_to_printable_text(html: &str, line_width: usize) -> String {
    let prepared = html
        .replace("</span><span", "</span>\t<span")
        .replace("</span> <span", "</span>\t<span")
        .replace("</span>\n<span", "</span>\t<span");

    let mut out = String::new();
    let mut chars = prepared.chars().peekable();
    let mut skip_tag_contents: Option<String> = None;

    while let Some(ch) = chars.next() {
        if ch == '<' {
            let mut tag_raw = String::new();
            for next in chars.by_ref() {
                if next == '>' {
                    break;
                }
                tag_raw.push(next);
            }

            let tag_trimmed = tag_raw.trim().to_lowercase();
            let is_closing = tag_trimmed.starts_with('/');
            let tag_name = if is_closing {
                tag_trimmed
                    .trim_start_matches('/')
                    .split_whitespace()
                    .next()
                    .unwrap_or("")
            } else {
                tag_trimmed.split_whitespace().next().unwrap_or("")
            };

            if let Some(skipped) = skip_tag_contents.as_ref() {
                if is_closing && tag_name == skipped {
                    skip_tag_contents = None;
                }
                continue;
            }

            if !is_closing && (tag_name == "style" || tag_name == "script" || tag_name == "svg") {
                skip_tag_contents = Some(tag_name.to_string());
                continue;
            }

            if tag_name == "br" {
                push_newline(&mut out);
                continue;
            }

            if matches!(
                tag_name,
                "div"
                    | "p"
                    | "h1"
                    | "h2"
                    | "h3"
                    | "h4"
                    | "h5"
                    | "h6"
                    | "li"
                    | "tr"
                    | "table"
                    | "section"
                    | "header"
                    | "footer"
                    | "ul"
                    | "ol"
            ) {
                push_newline(&mut out);
                continue;
            }

            if tag_name == "td" || tag_name == "th" {
                push_tab(&mut out);
                continue;
            }

            if tag_name == "hr" {
                push_newline(&mut out);
                out.push_str(&"-".repeat(line_width));
                push_newline(&mut out);
            }
        } else if skip_tag_contents.is_none() {
            out.push(ch);
        }
    }

    let decoded = decode_html_entities(&out);
    normalize_receipt_text_with_width(&decoded, line_width)
}

fn push_newline(out: &mut String) {
    if !out.ends_with('\n') {
        out.push('\n');
    }
}

fn push_tab(out: &mut String) {
    if out.ends_with('\n') || out.ends_with('\t') {
        return;
    }
    if out.ends_with(' ') {
        let _ = out.pop();
    }
    out.push('\t');
}

fn decode_html_entities(text: &str) -> String {
    text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&#39;", "'")
        .replace("&apos;", "'")
}

fn collapse_spaces_preserve_tabs(input: &str) -> String {
    let mut out = String::new();
    let mut prev_space = false;

    for ch in input.chars() {
        if ch == '\t' {
            if out.ends_with(' ') {
                let _ = out.pop();
            }
            if !out.ends_with('\t') {
                out.push('\t');
            }
            prev_space = false;
        } else if ch.is_whitespace() {
            if !prev_space {
                out.push(' ');
                prev_space = true;
            }
        } else {
            out.push(ch);
            prev_space = false;
        }
    }

    out.trim().to_string()
}

fn align_left_right(left: &str, right: &str, width: usize) -> String {
    let left = left.trim();
    let right = right.trim();

    if left.is_empty() {
        return right.to_string();
    }
    if right.is_empty() {
        return left.to_string();
    }

    let left_len = left.chars().count();
    let right_len = right.chars().count();
    if left_len + right_len + 1 >= width {
        format!("{} {}", left, right)
    } else {
        let spaces = " ".repeat(width - left_len - right_len);
        format!("{}{}{}", left, spaces, right)
    }
}

fn center_text(text: &str, width: usize) -> String {
    let value = text.trim();
    let len = value.chars().count();
    if len >= width {
        return value.to_string();
    }
    let pad_left = (width - len) / 2;
    format!("{}{}", " ".repeat(pad_left), value)
}

fn looks_like_amount(value: &str) -> bool {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return false;
    }

    let has_digit = trimmed.chars().any(|c| c.is_ascii_digit());
    let has_money_marker = trimmed.contains('.')
        || trimmed.contains(',')
        || trimmed.contains('$')
        || trimmed.contains('€')
        || trimmed.contains('£')
        || trimmed.contains('R');

    has_digit && has_money_marker
}

fn align_label_value(line: &str, width: usize) -> Option<String> {
    let colon_index = line.find(':')?;
    let label = line[..=colon_index].trim();
    let value = line[colon_index + 1..].trim();
    if label.is_empty() || value.is_empty() {
        return None;
    }
    Some(align_left_right(label, value, width))
}

fn looks_like_item_detail(line: &str) -> bool {
    let trimmed = line.trim();
    let mut chars = trimmed.chars();
    let first_is_digit = chars.next().map(|c| c.is_ascii_digit()).unwrap_or(false);
    first_is_digit && trimmed.contains('x')
}

fn is_amount_chunk(value: &str) -> bool {
    let trimmed = value.trim();
    if trimmed.is_empty() || !trimmed.chars().any(|c| c.is_ascii_digit()) {
        return false;
    }

    trimmed.chars().all(|c| {
        c.is_ascii_digit()
            || c.is_whitespace()
            || matches!(
                c,
                '.' | ',' | '$' | '€' | '£' | '-' | '+' | '(' | ')' | ':' | '%'
            )
            || matches!(c, 'R' | 'r' | 'S' | 's' | 'M' | 'm' | 'U' | 'u')
    })
}

fn split_trailing_amount(line: &str) -> Option<(String, String)> {
    let trimmed = line.trim();
    let mut best: Option<(String, String, usize)> = None;

    for (idx, ch) in trimmed.char_indices() {
        if !ch.is_whitespace() {
            continue;
        }

        let left = trimmed[..idx].trim();
        let right = trimmed[idx + ch.len_utf8()..].trim();
        if left.is_empty() || right.is_empty() || !is_amount_chunk(right) {
            continue;
        }

        let score = right.chars().count();
        match &best {
            Some((_, _, best_score)) if *best_score >= score => {}
            _ => best = Some((left.to_string(), right.to_string(), score)),
        }
    }

    best.map(|(left, right, _)| (left, right))
}

fn split_item_count_tail(line: &str) -> Option<(String, String)> {
    let tokens: Vec<&str> = line.split_whitespace().collect();
    if tokens.len() < 2 {
        return None;
    }

    let last = tokens[tokens.len() - 1];
    let prev = tokens[tokens.len() - 2];
    let last_lower = last.to_lowercase();
    if !(last_lower == "item" || last_lower == "items") {
        return None;
    }
    if !prev.chars().all(|c| c.is_ascii_digit()) {
        return None;
    }

    let left = tokens[..tokens.len() - 2].join(" ");
    if left.trim().is_empty() {
        return None;
    }

    Some((left, format!("{} {}", prev, last)))
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum ReceiptSection {
    Copy,
    Company,
    OrderInfo,
    Eis,
    Items,
    Tax,
    Totals,
    Footer,
    Other,
}

fn infer_section(line: &str, current: ReceiptSection) -> ReceiptSection {
    let lower = line.trim().to_lowercase();

    if lower.is_empty() {
        return current;
    }
    if is_legal_footer_line(&lower) {
        return ReceiptSection::Footer;
    }
    if lower.contains("*** copy #") || is_copy_marker_line(&lower) {
        return ReceiptSection::Copy;
    }
    if lower.starts_with("order #:")
        || lower.starts_with("receipt no:")
        || lower.starts_with("date:")
        || lower.starts_with("cashier:")
        || lower.starts_with("payment:")
        || lower == "invoice"
    {
        return ReceiptSection::OrderInfo;
    }
    if lower == "buyer details" {
        return ReceiptSection::Other;
    }
    if lower.starts_with("fiscal invoice:")
        || lower.starts_with("transmission:")
        || lower.starts_with("eis status:")
        || lower.starts_with("eis uuid:")
        || lower.starts_with("submitted at:")
        || lower.starts_with("seller tin:")
        || lower.starts_with("signature:")
    {
        return ReceiptSection::Eis;
    }
    if lower == "item total" || lower == "item" || lower == "total item" {
        return ReceiptSection::Items;
    }
    if lower == "items" {
        return ReceiptSection::Items;
    }
    if lower.starts_with("tax breakdown")
        || lower.starts_with("vat summary")
        || lower == "tax summary"
        || lower.starts_with("vat @")
        || lower.starts_with("vat ")
        || lower.starts_with("taxable value:")
        || lower.starts_with("taxable:")
        || lower.starts_with("vat amount:")
    {
        return ReceiptSection::Tax;
    }
    if lower.starts_with("subtotal:")
        || lower.starts_with("vat (")
        || lower.starts_with("total vat:")
        || lower == "total amount"
        || lower.starts_with("tip:")
        || lower.starts_with("total payable:")
        || lower.starts_with("total:")
    {
        return ReceiptSection::Totals;
    }
    if lower.starts_with("thank you")
        || lower.starts_with("powered by")
        || lower.starts_with("scan:")
    {
        return ReceiptSection::Footer;
    }

    if current == ReceiptSection::Copy {
        return ReceiptSection::Company;
    }

    match current {
        ReceiptSection::Items => {
            if looks_like_item_detail(line)
                || line.contains('\t')
                || looks_like_amount(line)
                || split_trailing_amount(line).is_some()
            {
                ReceiptSection::Items
            } else {
                ReceiptSection::Other
            }
        }
        _ => current,
    }
}

fn is_legal_footer_line(lower: &str) -> bool {
    (lower.starts_with("date:") && lower.contains("time:"))
        || lower.starts_with("scan here for receipt details")
        || lower.starts_with("mra qr pending")
        || lower.starts_with("*** end of legal receipt")
}

fn format_line_by_section(
    line: &str,
    section: ReceiptSection,
    line_width: usize,
) -> Option<String> {
    let trimmed = line.trim();
    if trimmed.is_empty() {
        return None;
    }

    if section == ReceiptSection::Copy {
        return Some(center_text(trimmed, line_width));
    }

    if is_dotted_rule(trimmed) || is_section_heading(trimmed) {
        return Some(center_text(trimmed, line_width));
    }

    if is_legal_receipt_marker(trimmed) {
        return Some(center_text(trimmed, line_width));
    }

    if section == ReceiptSection::Footer {
        return Some(center_text(trimmed, line_width));
    }

    // Normalize item heading rows produced by browser-rendered HTML.
    if matches!(section, ReceiptSection::Items)
        && (trimmed.eq_ignore_ascii_case("item total")
            || trimmed.eq_ignore_ascii_case("item")
            || trimmed.eq_ignore_ascii_case("total item"))
    {
        return Some(align_left_right("ITEM", "TOTAL", line_width));
    }
    if matches!(section, ReceiptSection::Tax)
        && (trimmed.to_lowercase().starts_with("tax breakdown")
            || trimmed.eq_ignore_ascii_case("vat summary"))
    {
        return None;
    }

    if trimmed.contains('\t') {
        let mut parts = trimmed
            .split('\t')
            .map(|p| p.trim())
            .filter(|p| !p.is_empty());
        let left = parts.next().unwrap_or("");
        let right = parts.next().unwrap_or("");
        if !left.is_empty() && !right.is_empty() {
            return Some(align_left_right(left, right, line_width));
        }
    }

    if section == ReceiptSection::Items {
        if let Some((left, right)) = split_trailing_amount(trimmed) {
            return Some(align_left_right(&left, &right, line_width));
        }
    }

    if section == ReceiptSection::Tax {
        if let Some((left, right)) = split_item_count_tail(trimmed) {
            return Some(align_left_right(&left, &right, line_width));
        }
        if let Some((left, right)) = split_trailing_amount(trimmed) {
            return Some(align_left_right(&left, &right, line_width));
        }
    }

    if section == ReceiptSection::Totals {
        if let Some((left, right)) = split_trailing_amount(trimmed) {
            return Some(align_left_right(&left, &right, line_width));
        }
    }

    if let Some(aligned) = align_label_value(trimmed, line_width) {
        return Some(aligned);
    }

    if section == ReceiptSection::Items && looks_like_item_detail(trimmed) {
        return Some(format!("  {}", trimmed));
    }

    if section == ReceiptSection::Totals && looks_like_amount(trimmed) {
        return Some(align_left_right("", trimmed, line_width));
    }

    if section == ReceiptSection::Company {
        return Some(center_text(trimmed, line_width));
    }

    Some(trimmed.to_string())
}

fn apply_professional_grouping(lines: Vec<String>, line_width: usize) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    let mut current = ReceiptSection::Company;

    for line in lines {
        let next = infer_section(&line, current);
        current = next;

        if let Some(formatted) = format_line_by_section(&line, current, line_width) {
            out.push(formatted);
        }
    }

    out
}

fn wrap_line_hard(line: &str, line_width: usize) -> Vec<String> {
    if line_width == 0 {
        return vec![line.to_string()];
    }

    let line_len = line.chars().count();
    if line_len <= line_width {
        return vec![line.to_string()];
    }

    let mut wrapped: Vec<String> = Vec::new();
    let mut chunk = String::new();
    let mut chunk_len = 0usize;

    for ch in line.chars() {
        chunk.push(ch);
        chunk_len += 1;

        if chunk_len >= line_width {
            wrapped.push(chunk);
            chunk = String::new();
            chunk_len = 0;
        }
    }

    if !chunk.is_empty() {
        wrapped.push(chunk);
    }

    wrapped
}

fn wrap_receipt_lines(lines: Vec<String>, line_width: usize) -> Vec<String> {
    let mut wrapped: Vec<String> = Vec::new();

    for line in lines {
        if line.trim().is_empty() {
            if wrapped
                .last()
                .map(|value| !value.trim().is_empty())
                .unwrap_or(false)
            {
                wrapped.push(String::new());
            }
            continue;
        }

        wrapped.extend(wrap_line_hard(&line, line_width));
    }

    wrapped
}

#[cfg(test)]
fn normalize_receipt_text(raw: &str) -> String {
    normalize_receipt_text_with_width(raw, DEFAULT_RECEIPT_LINE_WIDTH)
}

fn normalize_receipt_text_with_width(raw: &str, line_width: usize) -> String {
    let mut lines: Vec<String> = Vec::new();
    let mut pending_blank = false;

    for raw_line in raw.replace('\r', "").lines() {
        let collapsed = collapse_spaces_preserve_tabs(raw_line);

        if collapsed.is_empty() {
            pending_blank = true;
            continue;
        }

        if pending_blank && !lines.is_empty() {
            lines.push(String::new());
        }
        pending_blank = false;

        let parts: Vec<&str> = collapsed
            .split('\t')
            .map(|p| p.trim())
            .filter(|p| !p.is_empty())
            .collect();

        if parts.len() >= 2 {
            lines.push(align_left_right(parts[0], parts[1], line_width));
        } else {
            lines.push(collapsed);
        }
    }

    let grouped = apply_professional_grouping(lines, line_width);
    let wrapped = wrap_receipt_lines(grouped, line_width);
    wrapped.join("\n")
}

fn extract_qr_payload(html: &str) -> Option<String> {
    for attr in [
        "data-eis-qr-payload",
        "data-qr-payload",
        "data-eis-validation-url",
    ] {
        if let Some(value) = extract_attribute_value(html, attr) {
            let decoded = decode_html_entities(&value);
            let payload = decoded.trim();
            if !payload.is_empty() {
                return Some(payload.to_string());
            }
        }
    }

    for quote in ['"', '\''] {
        let needle = format!("src={}", quote);
        let mut offset = 0usize;

        while let Some(found) = html[offset..].find(&needle) {
            let start = offset + found + needle.len();
            let tail = &html[start..];
            let Some(end) = tail.find(quote) else {
                break;
            };
            let src = &tail[..end];

            if let Some(payload) = extract_qr_payload_from_src(src) {
                return Some(payload);
            }

            offset = start + end + 1;
        }
    }

    None
}

fn extract_thermal_receipt_text(html: &str) -> Option<String> {
    let encoded = extract_attribute_value(html, "data-thermal-receipt-text")?;
    let decoded = urlencoding::decode(&encoded).ok()?;
    let value = decoded.replace('\r', "").trim().to_string();
    if value.is_empty() {
        None
    } else {
        Some(value)
    }
}

fn extract_attribute_value(html: &str, attr_name: &str) -> Option<String> {
    for quote in ['"', '\''] {
        let needle = format!("{}={}", attr_name, quote);
        let mut offset = 0usize;

        while let Some(found) = html[offset..].find(&needle) {
            let start = offset + found + needle.len();
            let tail = &html[start..];
            let Some(end) = tail.find(quote) else {
                break;
            };
            let value = tail[..end].trim();
            if !value.is_empty() {
                return Some(value.to_string());
            }

            offset = start + end + 1;
        }
    }

    None
}

fn extract_qr_payload_from_src(src: &str) -> Option<String> {
    let src_lower = src.to_lowercase();
    if !src_lower.contains("qr") {
        return None;
    }

    if let Some(data_pos) = src.find("data=") {
        let encoded = &src[data_pos + 5..];
        let encoded_value = encoded.split('&').next().unwrap_or("").trim();
        if !encoded_value.is_empty() {
            if let Ok(decoded) = urlencoding::decode(encoded_value) {
                let value = decoded.trim().to_string();
                if !value.is_empty() {
                    return Some(value);
                }
            }
        }
    }

    None
}

fn append_qr_code(data: &mut Vec<u8>, payload: &str, horizontal_offset: usize, line_width: usize) {
    let value = payload.trim();
    if value.is_empty() {
        return;
    }

    let bytes = value.as_bytes();
    if bytes.is_empty() {
        return;
    }

    // ESC/POS QR payload max is bounded by pL/pH command packet size.
    let max_len = 7089usize;
    let qr_bytes = if bytes.len() > max_len {
        &bytes[..max_len]
    } else {
        bytes
    };

    data.extend_from_slice(b"\n");
    data.extend_from_slice(b"\x1D\x4C\x00\x00"); // reset left margin before centered QR
    data.extend_from_slice(b"\x1B\x61\x01"); // center align
    data.extend_from_slice(&[0x1D, 0x28, 0x6B, 0x04, 0x00, 0x31, 0x41, 0x32, 0x00]); // model 2
    let qr_module_size: u8 = if line_width <= COMPACT_RECEIPT_LINE_WIDTH {
        0x04
    } else {
        0x05
    };
    data.extend_from_slice(&[0x1D, 0x28, 0x6B, 0x03, 0x00, 0x31, 0x43, qr_module_size]); // size
    data.extend_from_slice(&[0x1D, 0x28, 0x6B, 0x03, 0x00, 0x31, 0x45, 0x31]); // error correction: M

    let payload_len = qr_bytes.len() + 3;
    let p_l = (payload_len & 0xFF) as u8;
    let p_h = ((payload_len >> 8) & 0xFF) as u8;
    data.extend_from_slice(&[0x1D, 0x28, 0x6B, p_l, p_h, 0x31, 0x50, 0x30]); // store data
    data.extend_from_slice(qr_bytes);
    data.extend_from_slice(&[0x1D, 0x28, 0x6B, 0x03, 0x00, 0x31, 0x51, 0x30]); // print
    data.extend_from_slice(b"\n");
    data.extend_from_slice(b"\x1B\x61\x00"); // left align
    let _ = horizontal_offset;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn copy_line_does_not_swallow_company_section() {
        let raw = "COPY #2\nMy Test Store\nOrder #: 1001";
        let normalized = normalize_receipt_text(raw);

        assert!(normalized.contains("My Test Store"));
        assert!(normalized.contains("Order #:"));
    }

    #[test]
    fn aligns_label_values_and_trailing_amounts() {
        let raw = "Subtotal:\tRs 75.00\nBread Rs 30.00\nTOTAL:\tRs 75.00";
        let normalized = normalize_receipt_text(raw);
        let lines: Vec<&str> = normalized.lines().collect();

        assert!(lines
            .iter()
            .any(|line| line.contains("Subtotal:") && line.ends_with("Rs 75.00")));
        assert!(lines
            .iter()
            .any(|line| line.contains("Bread") && line.ends_with("Rs 30.00")));
        assert!(lines
            .iter()
            .any(|line| line.contains("TOTAL:") && line.ends_with("Rs 75.00")));
    }

    #[test]
    fn extracts_qr_payload_from_receipt_wrapper_attribute() {
        let html = r#"<div id="receipt-printable-area" data-eis-qr-payload="https://dev-eis-portal.mra.mw/ReceiptValidation/Validate/CuQ-D-JY4P-D?tin=70267581&amp;mode=online"><p>Receipt</p></div>"#;
        let payload = extract_qr_payload(html);

        assert_eq!(
            payload.as_deref(),
            Some("https://dev-eis-portal.mra.mw/ReceiptValidation/Validate/CuQ-D-JY4P-D?tin=70267581&mode=online")
        );
    }

    #[test]
    fn centers_legal_receipt_footer_from_date_time_line() {
        let raw = "TOTAL:\tMWK 250.00\nDATE: 2026-05-23 TIME: 12:33:37\nScan Here For Receipt Details\n*** END OF LEGAL RECEIPT ***\nTHANK YOU!";
        let normalized = normalize_receipt_text_with_width(raw, DEFAULT_RECEIPT_LINE_WIDTH);
        let lines: Vec<&str> = normalized.lines().collect();

        assert!(lines.iter().any(|line| line.starts_with("TOTAL:")));
        for expected in [
            "DATE: 2026-05-23 TIME: 12:33:37",
            "Scan Here For Receipt Details",
            "*** END OF LEGAL RECEIPT ***",
        ] {
            let line = lines
                .iter()
                .find(|line| line.trim() == expected)
                .unwrap_or_else(|| panic!("missing centered footer line: {}", expected));
            assert!(
                line.starts_with(' '),
                "footer line was not centered: {}",
                line
            );
        }
    }

    #[test]
    fn centers_legal_receipt_start_marker_without_banner_styling() {
        let raw = "*** START OF LEGAL RECEIPT ***\nHANDYPOS\nTIN: 70267581\n*VAT REGISTERED*\nReceipt Number:\tCuQ-H-JY4Z-B";
        let normalized = normalize_receipt_text_with_width(raw, DEFAULT_RECEIPT_LINE_WIDTH);
        let lines: Vec<&str> = normalized.lines().collect();
        let start_line = lines
            .iter()
            .find(|line| line.trim() == "*** START OF LEGAL RECEIPT ***")
            .expect("missing legal receipt start marker");
        let vat_line = lines
            .iter()
            .find(|line| line.trim() == "*VAT REGISTERED*")
            .expect("missing VAT registration marker");

        assert!(
            start_line.starts_with(' '),
            "start marker was not centered: {start_line}"
        );
        assert!(
            vat_line.starts_with(' '),
            "VAT marker was not centered: {vat_line}"
        );
    }

    #[test]
    fn prints_qr_between_scan_instruction_and_legal_receipt_end() {
        let payload = "https://dev-eis-portal.mra.mw/ReceiptValidation/Validate/CuQ-D-JY4P-D";
        let html = format!(
            r#"<div id="receipt-printable-area" data-eis-qr-payload="{payload}"><p>DATE: 2026-05-23 TIME: 12:33:37</p><p>Scan Here For Receipt Details</p><p>*** END OF LEGAL RECEIPT ***</p></div>"#
        );
        let bytes = html_to_escpos(&html, DEFAULT_RECEIPT_LINE_WIDTH, 0);
        let rendered = String::from_utf8_lossy(&bytes);

        let scan_index = rendered.find("Scan Here For Receipt Details").unwrap();
        let payload_index = rendered.find(payload).unwrap();
        let end_index = rendered.find("*** END OF LEGAL RECEIPT ***").unwrap();

        assert!(scan_index < payload_index);
        assert!(payload_index < end_index);
    }

    #[test]
    fn thermal_receipt_text_attribute_overrides_css_html_layout() {
        let thermal = "*** START OF LEGAL RECEIPT ***\n1 X 19350.00                 19350.00 A\nCOKE 01X20 300 R...\nTOTAL:                       19350.00\nScan Here For Receipt Details\n*** END OF LEGAL RECEIPT ***";
        let encoded = urlencoding::encode(thermal);
        let html = format!(
            r#"<div id="receipt-printable-area" data-thermal-receipt-text="{encoded}" data-eis-qr-payload="https://example.test/qr"><div style="display:flex"><span>broken css</span><span>layout</span></div></div>"#
        );
        let bytes = html_to_escpos(&html, DEFAULT_RECEIPT_LINE_WIDTH, 0);
        let rendered = String::from_utf8_lossy(&bytes);

        assert!(rendered.contains("1 X 19350.00                 19350.00 A"));
        assert!(rendered.contains("TOTAL:                       19350.00"));
        assert!(!rendered.contains("broken css"));
    }

    #[test]
    fn explicit_thermal_receipt_bolds_vat_status_and_total() {
        let thermal = "*** START OF LEGAL RECEIPT ***\nHANDYPOS\nTIN: 70267581\n*VAT REGISTERED*\nTOTAL:                       19350.00\n*** END OF LEGAL RECEIPT ***";
        let encoded = urlencoding::encode(thermal);
        let html = format!(
            r#"<div id="receipt-printable-area" data-thermal-receipt-text="{encoded}"></div>"#
        );
        let bytes = html_to_escpos(&html, DEFAULT_RECEIPT_LINE_WIDTH, 0);
        let rendered = String::from_utf8_lossy(&bytes);

        let bold_on = "\x1B\x45\x01";
        let vat_index = rendered.find("*VAT REGISTERED*").unwrap();
        let total_index = rendered.find("TOTAL:").unwrap();
        let vat_bold_index = rendered[..vat_index].rfind(bold_on).unwrap();
        let total_bold_index = rendered[..total_index].rfind(bold_on).unwrap();

        assert!(vat_bold_index < vat_index);
        assert!(total_bold_index < total_index);
    }

    #[test]
    fn explicit_thermal_receipt_preserves_centered_legal_and_vat_padding() {
        let start = center_text("*** START OF LEGAL RECEIPT ***", DEFAULT_RECEIPT_LINE_WIDTH);
        let vat = center_text("*VAT REGISTERED*", DEFAULT_RECEIPT_LINE_WIDTH);
        let end = center_text("*** END OF LEGAL RECEIPT ***", DEFAULT_RECEIPT_LINE_WIDTH);
        let thermal = format!("{start}\n{vat}\nTOTAL:                       19350.00\n{end}");
        let encoded = urlencoding::encode(thermal);
        let html = format!(
            r#"<div id="receipt-printable-area" data-thermal-receipt-text="{encoded}"></div>"#
        );
        let bytes = html_to_escpos(&html, DEFAULT_RECEIPT_LINE_WIDTH, 0);
        let rendered = String::from_utf8_lossy(&bytes);

        assert!(rendered.contains(&format!("{start}\n")));
        assert!(rendered.contains(&format!("{vat}\n")));
        assert!(rendered.contains(&format!("{end}\n")));
    }

    #[test]
    fn explicit_thermal_receipt_preserves_centered_markers_for_58mm_width() {
        let start = center_text("*** START OF LEGAL RECEIPT ***", COMPACT_RECEIPT_LINE_WIDTH);
        let vat = center_text("*NON VAT REGISTERED*", COMPACT_RECEIPT_LINE_WIDTH);
        let end = center_text("*** END OF LEGAL RECEIPT ***", COMPACT_RECEIPT_LINE_WIDTH);
        let thermal = format!("{start}\n{vat}\nTOTAL:             19350.00\n{end}");
        let encoded = urlencoding::encode(thermal);
        let html = format!(
            r#"<div id="receipt-printable-area" data-thermal-receipt-text="{encoded}"></div>"#
        );
        let bytes = html_to_escpos(&html, COMPACT_RECEIPT_LINE_WIDTH, 0);
        let rendered = String::from_utf8_lossy(&bytes);

        assert!(rendered.contains(&format!("{start}\n")));
        assert!(rendered.contains(&format!("{vat}\n")));
        assert!(rendered.contains(&format!("{end}\n")));
    }

    #[test]
    fn qr_printing_uses_center_alignment_command() {
        let payload = "https://example.test/qr";
        let html = format!(r#"<div data-eis-qr-payload="{payload}"></div>"#);
        let bytes = html_to_escpos(&html, DEFAULT_RECEIPT_LINE_WIDTH, 0);
        let rendered = String::from_utf8_lossy(&bytes);

        let center_on = "\x1B\x61\x01";
        let payload_index = rendered.find(payload).unwrap();
        let center_index = rendered[..payload_index].rfind(center_on).unwrap();

        assert!(center_index < payload_index);
    }

    #[test]
    fn explicit_thermal_receipt_preserves_centered_copy_marker_padding() {
        let thermal = "                   COPY\nBuyers Name:              Walk-in Customer";
        let encoded = urlencoding::encode(thermal);
        let html = format!(
            r#"<div id="receipt-printable-area" data-thermal-receipt-text="{encoded}"></div>"#
        );
        let bytes = html_to_escpos(&html, DEFAULT_RECEIPT_LINE_WIDTH, 0);
        let rendered = String::from_utf8_lossy(&bytes);

        assert!(rendered.contains("                   COPY\n"));
        assert!(rendered.contains("Buyers Name:              Walk-in Customer"));
    }
}
