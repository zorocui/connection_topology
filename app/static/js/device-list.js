(() => {
  const searchForm = document.getElementById("device-search-form");
  const searchInput = document.getElementById("device-search");
  const pageSize = document.getElementById("device-page-size");
  const jump = document.getElementById("device-page-jump");
  const jumpInput = document.getElementById("device-page-jump-input");
  const jumpButton = document.getElementById("device-page-jump-button");

  const navigateToPage = page => {
    const url = new URL(window.location.href);
    url.searchParams.set("page", String(page));
    window.location.assign(url);
  };

  pageSize?.addEventListener("change", () => {
    const url = new URL(window.location.href);
    url.searchParams.set("page_size", pageSize.value);
    url.searchParams.set("page", "1");
    window.location.assign(url);
  });

  searchInput?.addEventListener("keydown", event => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    searchForm?.requestSubmit();
  });

  const goToRequestedPage = () => {
    const rawValue = jumpInput?.value.trim() || "";
    if (!/^\d+$/.test(rawValue)) {
      return toast("请输入有效页码", "error");
    }
    const totalPages = Number(jump.dataset.totalPages);
    const requestedPage = Number(rawValue);
    const targetPage = Math.min(Math.max(requestedPage, 1), totalPages);
    navigateToPage(targetPage);
  };

  jumpButton?.addEventListener("click", goToRequestedPage);
  jumpInput?.addEventListener("keydown", event => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    goToRequestedPage();
  });
})();
