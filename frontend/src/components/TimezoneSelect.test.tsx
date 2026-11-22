import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import "../i18n";
import { TimezoneSelect } from "./TimezoneSelect";

describe("TimezoneSelect", () => {
  it("offers the zones as a list, the common ones first", () => {
    const change = vi.fn();
    render(
      <label>
        Zone
        <TimezoneSelect value="Africa/Algiers" onChange={change} />
      </label>,
    );
    const select = screen.getByLabelText("Zone") as HTMLSelectElement;
    expect(select.tagName).toBe("SELECT");
    expect(select.value).toBe("Africa/Algiers");
    const groups = select.querySelectorAll("optgroup");
    expect(groups[0]?.querySelectorAll("option")[0]?.value).toBe(
      "Africa/Algiers",
    );
    expect([...select.options].some((o) => o.value === "Europe/Paris")).toBe(
      true,
    );
    fireEvent.change(select, { target: { value: "UTC" } });
    expect(change).toHaveBeenCalledWith("UTC");
  });

  it("keeps a saved zone the browser does not list", () => {
    render(<TimezoneSelect value="Mars/Olympus" onChange={() => undefined} />);
    expect((screen.getByRole("combobox") as HTMLSelectElement).value).toBe(
      "Mars/Olympus",
    );
  });
});
