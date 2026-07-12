export default {
  template: `
    <q-uploader
      ref="qRef"
      :url="computed_url"
      :factory="hasBridge ? bridgeFactory : null"
    >
      <template v-for="(_, slot) in $slots" v-slot:[slot]="slotProps">
        <slot :name="slot" v-bind="slotProps || {}" />
      </template>
    </q-uploader>
  `,
  mounted() {
    setTimeout(() => this.compute_url(), 0); // wait for window.path_prefix to be set in app.mounted()
  },
  updated() {
    this.compute_url();
  },
  methods: {
    compute_url() {
      this.computed_url = (this.url.startsWith("/") ? window.path_prefix : "") + this.url;
    },
    bridgeFactory(files) {
      const id = this.url.split("/").pop();
      Promise.all(
        Array.from(files).map(
          (file) =>
            new Promise((resolve) => {
              const reader = new FileReader();
              reader.onload = () =>
                resolve({ name: file.name, type: file.type, data: reader.result.split(",")[1] });
              reader.readAsDataURL(file);
            })
        )
      ).then((encoded) => {
        window.niceguiBridge.onUpload(JSON.stringify({ id, files: encoded }));
        this.$refs.qRef.reset();
      });
      return Promise.reject();
    },
  },
  props: {
    url: String,
  },
  data: function () {
    return {
      hasBridge: !!window.niceguiBridge,
      computed_url: this.url,
    };
  },
};
