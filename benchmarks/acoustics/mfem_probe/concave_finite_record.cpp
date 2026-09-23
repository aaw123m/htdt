#include "mfem.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

namespace
{

struct TimeSample
{
   double pressure_pa = 0.0;
   double source_volume_velocity_m3_s = 0.0;
};


void WriteSparseMatrixJson(std::ofstream &os, const char *name, mfem::SparseMatrix &matrix)
{
   const int rows = matrix.Height();
   const int cols = matrix.Width();
   const int nnz = matrix.NumNonZeroElems();
   const int *row_offsets = matrix.GetI();
   const int *column_indices = matrix.GetJ();
   const double *values = matrix.GetData();

   os << "  \"" << name << "\": {\n";
   os << "    \"rows\": " << rows << ",\n";
   os << "    \"cols\": " << cols << ",\n";
   os << "    \"nnz\": " << nnz << ",\n";
   os << "    \"row_offsets\": [";
   for (int i = 0; i <= rows; ++i)
   {
      if (i) { os << ", "; }
      os << row_offsets[i];
   }
   os << "],\n";
   os << "    \"column_indices\": [";
   for (int i = 0; i < nnz; ++i)
   {
      if (i) { os << ", "; }
      os << column_indices[i];
   }
   os << "],\n";
   os << "    \"values\": [";
   for (int i = 0; i < nnz; ++i)
   {
      if (i) { os << ", "; }
      os << values[i];
   }
   os << "]\n";
   os << "  }";
}

void WriteVectorJson(std::ofstream &os, const char *name, const mfem::Vector &vector)
{
   os << "  \"" << name << "\": [";
   for (int i = 0; i < vector.Size(); ++i)
   {
      if (i) { os << ", "; }
      os << vector[i];
   }
   os << "]";
}

void WriteSystemJson(
   const std::string &path,
   int order,
   int uniform_refinements,
   int elements,
   int ndofs,
   double density_kg_m3,
   double sound_speed_m_s,
   double source_x,
   double source_y,
   double source_z,
   double receiver_x,
   double receiver_y,
   double receiver_z,
   mfem::SparseMatrix &mass,
   mfem::SparseMatrix &stiffness_c2,
   const mfem::Vector &source_functional,
   const mfem::Vector &receiver_functional)
{
   std::ofstream os(path, std::ios::binary);
   if (!os)
   {
      throw std::runtime_error("cannot open semidiscrete system output file: " + path);
   }

   os << std::setprecision(17);
   os << "{\n";
   os << "  \"schema_version\": \"r100b-mfem-concave-semidiscrete-system-1\",\n";
   os << "  \"mfem_version\": \"" << MFEM_VERSION_STRING << "\",\n";
   os << "  \"fixture_id\": \"wave-concave-l-room-v1\",\n";
   os << "  \"geometry\": \"exact-five-hex-l-prism\",\n";
   os << "  \"boundary_model\": \"natural-neumann-rigid\",\n";
   os << "  \"primary_field\": \"velocity_potential_phi\",\n";
   os << "  \"governing_equation\": \"M*phi_tt+Kc2*phi=c^2*b*q\",\n";
   os << "  \"mass_assembly\": \"MFEM MassIntegrator\",\n";
   os << "  \"stiffness_assembly\": \"MFEM DiffusionIntegrator(c^2)\",\n";
   os << "  \"source_functional_assembly\": \"MFEM DomainLFIntegrator(DeltaCoefficient)\",\n";
   os << "  \"receiver_functional_assembly\": \"MFEM DomainLFIntegrator(DeltaCoefficient)\",\n";
   os << "  \"matrix_format\": \"csr_full\",\n";
   os << "  \"order\": " << order << ",\n";
   os << "  \"uniform_refinements\": " << uniform_refinements << ",\n";
   os << "  \"elements\": " << elements << ",\n";
   os << "  \"ndofs\": " << ndofs << ",\n";
   os << "  \"density_kg_m3\": " << density_kg_m3 << ",\n";
   os << "  \"sound_speed_m_s\": " << sound_speed_m_s << ",\n";
   os << "  \"source_position_m\": ["
      << source_x << ", " << source_y << ", " << source_z << "],\n";
   os << "  \"receiver_position_m\": ["
      << receiver_x << ", " << receiver_y << ", " << receiver_z << "],\n";
   os << "  \"source_normalization\": \"volume_velocity_m3_s\",\n";
   WriteSparseMatrixJson(os, "mass_matrix", mass);
   os << ",\n";
   WriteSparseMatrixJson(os, "stiffness_c2_matrix", stiffness_c2);
   os << ",\n";
   WriteVectorJson(os, "source_functional", source_functional);
   os << ",\n";
   WriteVectorJson(os, "receiver_functional", receiver_functional);
   os << "\n}\n";
}

mfem::Mesh BuildFrozenConcaveLRoom()
{
   // Exact wave-concave-l-room-v1 prism. Five conforming 2 m x 2 m x 2.5 m
   // hexahedra tile only the L-shaped acoustic volume; the concave notch is
   // deliberately absent.
   mfem::Mesh mesh(3, 24, 5, 0, 3);

   const double xs[] = {0.0, 2.0, 4.0, 6.0};
   const double ys[] = {0.0, 2.0, 4.0};
   const double zs[] = {0.0, 2.5};

   for (double z : zs)
   {
      for (double y : ys)
      {
         for (double x : xs)
         {
            mesh.AddVertex(x, y, z);
         }
      }
   }

   auto vid = [](int ix, int iy, int iz)
   {
      return iz * 12 + iy * 4 + ix;
   };

   auto add_cell = [&](int ix, int iy)
   {
      mesh.AddHex(
         vid(ix,     iy,     0),
         vid(ix + 1, iy,     0),
         vid(ix + 1, iy + 1, 0),
         vid(ix,     iy + 1, 0),
         vid(ix,     iy,     1),
         vid(ix + 1, iy,     1),
         vid(ix + 1, iy + 1, 1),
         vid(ix,     iy + 1, 1),
         1);
   };

   add_cell(0, 0);
   add_cell(1, 0);
   add_cell(2, 0);
   add_cell(0, 1);
   add_cell(2, 1);

   mesh.FinalizeHexMesh(1, 0, true);
   return mesh;
}

class HomogeneousPotentialWaveOperator : public mfem::SecondOrderTimeDependentOperator
{
private:
   mfem::SparseMatrix &mass_;
   mfem::SparseMatrix &stiffness_;
   mfem::CGSolver mass_solver_;
   mfem::DSmoother mass_prec_;
   mfem::CGSolver implicit_solver_;
   mfem::DSmoother implicit_prec_;
   std::unique_ptr<mfem::SparseMatrix> implicit_matrix_;
   double current_fac0_ = -1.0;
   mutable mfem::Vector work_;
   double max_relative_residual_ = 0.0;
   int max_iterations_ = 0;

   static double RelativeResidual(
      mfem::SparseMatrix &matrix,
      const mfem::Vector &x,
      const mfem::Vector &rhs)
   {
      mfem::Vector residual(rhs.Size());
      matrix.Mult(x, residual);
      residual -= rhs;
      const double rhs_norm = rhs.Norml2();
      const double residual_norm = residual.Norml2();
      return residual_norm / std::max(rhs_norm, 1.0e-30);
   }

public:
   HomogeneousPotentialWaveOperator(
      mfem::SparseMatrix &mass,
      mfem::SparseMatrix &stiffness)
      : mfem::SecondOrderTimeDependentOperator(mass.Height(), 0.0),
        mass_(mass),
        stiffness_(stiffness),
        work_(mass.Height())
   {
      mass_solver_.iterative_mode = false;
      mass_solver_.SetRelTol(1.0e-12);
      mass_solver_.SetAbsTol(0.0);
      mass_solver_.SetMaxIter(1000);
      mass_solver_.SetPrintLevel(0);
      mass_solver_.SetPreconditioner(mass_prec_);
      mass_solver_.SetOperator(mass_);
   }

   using mfem::SecondOrderTimeDependentOperator::Mult;
   void Mult(
      const mfem::Vector &u,
      const mfem::Vector &,
      mfem::Vector &d2udt2) const override
   {
      stiffness_.Mult(u, work_);
      work_.Neg();
      mass_solver_.Mult(work_, d2udt2);
      if (!mass_solver_.GetConverged())
      {
         throw std::runtime_error("MFEM mass solve did not converge");
      }
   }

   using mfem::SecondOrderTimeDependentOperator::ImplicitSolve;
   void ImplicitSolve(
      const double fac0,
      const double,
      const mfem::Vector &u,
      const mfem::Vector &,
      mfem::Vector &d2udt2) override
   {
      if (!implicit_matrix_ || std::abs(fac0 - current_fac0_) > 1.0e-18)
      {
         implicit_matrix_.reset(mfem::Add(1.0, mass_, fac0, stiffness_));
         implicit_solver_.iterative_mode = false;
         implicit_solver_.SetRelTol(1.0e-12);
         implicit_solver_.SetAbsTol(0.0);
         implicit_solver_.SetMaxIter(1000);
         implicit_solver_.SetPrintLevel(0);
         implicit_solver_.SetPreconditioner(implicit_prec_);
         implicit_solver_.SetOperator(*implicit_matrix_);
         current_fac0_ = fac0;
      }

      stiffness_.Mult(u, work_);
      work_.Neg();
      implicit_solver_.Mult(work_, d2udt2);
      if (!implicit_solver_.GetConverged())
      {
         throw std::runtime_error("MFEM Newmark implicit solve did not converge");
      }

      max_iterations_ = std::max(max_iterations_, implicit_solver_.GetNumIterations());
      max_relative_residual_ = std::max(
         max_relative_residual_,
         RelativeResidual(*implicit_matrix_, d2udt2, work_));
   }

   double max_relative_residual() const { return max_relative_residual_; }
   int max_iterations() const { return max_iterations_; }
};

double SolveMassKick(
   mfem::SparseMatrix &mass,
   const mfem::Vector &source_functional,
   double scale,
   mfem::Vector &rate)
{
   mfem::Vector rhs(source_functional);
   rhs *= scale;

   mfem::DSmoother preconditioner;
   mfem::CGSolver solver;
   solver.iterative_mode = false;
   solver.SetRelTol(1.0e-12);
   solver.SetAbsTol(0.0);
   solver.SetMaxIter(1000);
   solver.SetPrintLevel(0);
   solver.SetPreconditioner(preconditioner);
   solver.SetOperator(mass);
   solver.Mult(rhs, rate);
   if (!solver.GetConverged())
   {
      throw std::runtime_error("MFEM source mass solve did not converge");
   }

   mfem::Vector residual(rhs.Size());
   mass.Mult(rate, residual);
   residual -= rhs;
   return residual.Norml2() / std::max(rhs.Norml2(), 1.0e-30);
}

void WriteJson(
   const std::string &path,
   int order,
   int uniform_refinements,
   int elements,
   int ndofs,
   double density_kg_m3,
   double sound_speed_m_s,
   double source_x,
   double source_y,
   double source_z,
   double receiver_x,
   double receiver_y,
   double receiver_z,
   double source_amplitude_m3_s,
   double observation_time_s,
   int sample_rate_hz,
   double dt_s,
   double assemble_s,
   double solve_s,
   double source_mass_relative_residual,
   double max_implicit_relative_residual,
   int max_implicit_iterations,
   const std::vector<TimeSample> &samples)
{
   std::ofstream os(path, std::ios::binary);
   if (!os)
   {
      throw std::runtime_error("cannot open output file: " + path);
   }

   os << std::setprecision(17);
   os << "{\n";
   os << "  \"schema_version\": \"r100b-mfem-concave-finite-record-raw-1\",\n";
   os << "  \"mfem_version\": \"" << MFEM_VERSION_STRING << "\",\n";
   os << "  \"fixture_id\": \"wave-concave-l-room-v1\",\n";
   os << "  \"geometry\": \"exact-five-hex-l-prism\",\n";
   os << "  \"boundary_model\": \"natural-neumann-rigid\",\n";
   os << "  \"primary_field\": \"velocity_potential_phi\",\n";
   os << "  \"governing_equation\": \"M*phi_tt+c^2*K*phi=c^2*b*q\",\n";
   os << "  \"pressure_conversion\": \"p=rho*d(phi)/dt\",\n";
   os << "  \"source_injection\": \"q[0] integrated as initial phi_t kick c^2*dt*M^-1*b*q0; q[n>0]=0\",\n";
   os << "  \"sample_zero_state\": \"after_source_t0_kick_before_first_homogeneous_step\",\n";
   os << "  \"time_integrator\": \"Newmark(beta=0.25,gamma=0.5)\",\n";
   os << "  \"algorithmic_damping\": \"none\",\n";
   os << "  \"window\": \"none\",\n";
   os << "  \"filter\": \"none\",\n";
   os << "  \"zero_padding\": \"none\",\n";
   os << "  \"order\": " << order << ",\n";
   os << "  \"uniform_refinements\": " << uniform_refinements << ",\n";
   os << "  \"elements\": " << elements << ",\n";
   os << "  \"ndofs\": " << ndofs << ",\n";
   os << "  \"density_kg_m3\": " << density_kg_m3 << ",\n";
   os << "  \"sound_speed_m_s\": " << sound_speed_m_s << ",\n";
   os << "  \"source_position_m\": ["
      << source_x << ", " << source_y << ", " << source_z << "],\n";
   os << "  \"receiver_position_m\": ["
      << receiver_x << ", " << receiver_y << ", " << receiver_z << "],\n";
   os << "  \"source_amplitude_m3_s\": " << source_amplitude_m3_s << ",\n";
   os << "  \"source_normalization\": \"volume_velocity_m3_s\",\n";
   os << "  \"source_t0_s\": 0,\n";
   os << "  \"record_interval\": \"half_open_0_T\",\n";
   os << "  \"observation_time_s\": " << observation_time_s << ",\n";
   os << "  \"sample_rate_hz\": " << sample_rate_hz << ",\n";
   os << "  \"dt_s\": " << dt_s << ",\n";
   os << "  \"sample_count\": " << samples.size() << ",\n";
   os << "  \"last_sample_time_s\": "
      << (samples.empty() ? 0.0 : (samples.size() - 1) * dt_s) << ",\n";
   os << "  \"assemble_s\": " << assemble_s << ",\n";
   os << "  \"solve_s\": " << solve_s << ",\n";
   os << "  \"linear_solver\": \"serial CG with diagonal Jacobi preconditioner\",\n";
   os << "  \"linear_relative_tolerance\": 1e-12,\n";
   os << "  \"linear_max_iterations\": 1000,\n";
   os << "  \"source_mass_relative_residual\": "
      << source_mass_relative_residual << ",\n";
   os << "  \"max_implicit_relative_residual\": "
      << max_implicit_relative_residual << ",\n";
   os << "  \"max_implicit_iterations\": "
      << max_implicit_iterations << ",\n";
   os << "  \"samples\": [\n";

   for (size_t index = 0; index < samples.size(); ++index)
   {
      const TimeSample &sample = samples[index];
      os << "    {"
         << "\"index\":" << index << ","
         << "\"time_s\":" << index * dt_s << ","
         << "\"pressure_pa\":" << sample.pressure_pa << ","
         << "\"source_volume_velocity_m3_s\":"
         << sample.source_volume_velocity_m3_s
         << "}" << (index + 1 == samples.size() ? "" : ",") << "\n";
   }

   os << "  ]\n";
   os << "}\n";
}

int ProbeMain(int argc, char *argv[])
{
   using clock = std::chrono::steady_clock;

   double density_kg_m3 = 1.2;
   double sound_speed_m_s = 343.0;
   double source_x = 1.0;
   double source_y = 1.0;
   double source_z = 1.0;
   double receiver_x = 5.0;
   double receiver_y = 1.0;
   double receiver_z = 1.0;
   double source_amplitude_m3_s = 1.0;
   double observation_time_s = 2.0;
   int sample_rate_hz = 6000;
   int order = 2;
   int uniform_refinements = 0;
   std::string output;
   std::string system_output;
   bool assemble_only = false;

   for (int i = 1; i < argc; ++i)
   {
      const std::string arg = argv[i];
      auto value = [&](const char *name)
      {
         if (i + 1 >= argc)
         {
            throw std::runtime_error(std::string("missing value for ") + name);
         }
         return std::string(argv[++i]);
      };

      if (arg == "--density") { density_kg_m3 = std::stod(value("--density")); }
      else if (arg == "--sound-speed") { sound_speed_m_s = std::stod(value("--sound-speed")); }
      else if (arg == "--source-x") { source_x = std::stod(value("--source-x")); }
      else if (arg == "--source-y") { source_y = std::stod(value("--source-y")); }
      else if (arg == "--source-z") { source_z = std::stod(value("--source-z")); }
      else if (arg == "--receiver-x") { receiver_x = std::stod(value("--receiver-x")); }
      else if (arg == "--receiver-y") { receiver_y = std::stod(value("--receiver-y")); }
      else if (arg == "--receiver-z") { receiver_z = std::stod(value("--receiver-z")); }
      else if (arg == "--source-amplitude") { source_amplitude_m3_s = std::stod(value("--source-amplitude")); }
      else if (arg == "--observation-time") { observation_time_s = std::stod(value("--observation-time")); }
      else if (arg == "--sample-rate-hz") { sample_rate_hz = std::stoi(value("--sample-rate-hz")); }
      else if (arg == "--order") { order = std::stoi(value("--order")); }
      else if (arg == "--uniform-refinements") { uniform_refinements = std::stoi(value("--uniform-refinements")); }
      else if (arg == "--output") { output = value("--output"); }
      else if (arg == "--system-output") { system_output = value("--system-output"); }
      else if (arg == "--assemble-only") { assemble_only = true; }
      else { throw std::runtime_error("unknown argument: " + arg); }
   }

   if (assemble_only && system_output.empty())
   {
      throw std::runtime_error("--system-output is required with --assemble-only");
   }
   if (!assemble_only && output.empty())
   {
      throw std::runtime_error("--output is required unless --assemble-only is used");
   }
   if (!(density_kg_m3 > 0.0 && sound_speed_m_s > 0.0
         && source_amplitude_m3_s > 0.0 && observation_time_s > 0.0))
   {
      throw std::runtime_error("physical scalar inputs must be positive");
   }
   if (sample_rate_hz <= 0 || order < 1 || order > 8
       || uniform_refinements < 0 || uniform_refinements > 3)
   {
      throw std::runtime_error("invalid discretization controls");
   }

   const double dt_s = 1.0 / static_cast<double>(sample_rate_hz);
   const double exact_count = observation_time_s * static_cast<double>(sample_rate_hz);
   const long long sample_count_ll = std::llround(exact_count);
   if (std::abs(exact_count - static_cast<double>(sample_count_ll)) > 1.0e-10)
   {
      throw std::runtime_error("observation time must contain an integer number of samples");
   }
   if (sample_count_ll < 2)
   {
      throw std::runtime_error("finite record requires at least two samples");
   }
   const size_t sample_count = static_cast<size_t>(sample_count_ll);

   mfem::Mesh mesh = BuildFrozenConcaveLRoom();
   for (int level = 0; level < uniform_refinements; ++level)
   {
      mesh.UniformRefinement();
   }

   mfem::H1_FECollection fec(order, 3);
   mfem::FiniteElementSpace fes(&mesh, &fec);

   const auto assemble_started = clock::now();

   mfem::BilinearForm mass(&fes);
   mass.AddDomainIntegrator(new mfem::MassIntegrator);
   mass.Assemble();
   mass.Finalize();

   mfem::ConstantCoefficient c2(sound_speed_m_s * sound_speed_m_s);
   mfem::BilinearForm stiffness(&fes);
   stiffness.AddDomainIntegrator(new mfem::DiffusionIntegrator(c2));
   stiffness.Assemble();
   stiffness.Finalize();

   mfem::DeltaCoefficient source_delta(source_x, source_y, source_z, 1.0);
   mfem::LinearForm source_functional(&fes);
   source_functional.AddDomainIntegrator(new mfem::DomainLFIntegrator(source_delta));
   source_functional.Assemble();

   mfem::DeltaCoefficient receiver_delta(receiver_x, receiver_y, receiver_z, 1.0);
   mfem::LinearForm receiver_functional(&fes);
   receiver_functional.AddDomainIntegrator(new mfem::DomainLFIntegrator(receiver_delta));
   receiver_functional.Assemble();

   const auto assemble_finished = clock::now();
   const double assemble_s =
      std::chrono::duration<double>(assemble_finished - assemble_started).count();

   mfem::SparseMatrix &M = mass.SpMat();
   mfem::SparseMatrix &Kc2 = stiffness.SpMat();

   if (!system_output.empty())
   {
      WriteSystemJson(
         system_output,
         order,
         uniform_refinements,
         mesh.GetNE(),
         fes.GetTrueVSize(),
         density_kg_m3,
         sound_speed_m_s,
         source_x,
         source_y,
         source_z,
         receiver_x,
         receiver_y,
         receiver_z,
         M,
         Kc2,
         source_functional,
         receiver_functional);
   }
   if (assemble_only)
   {
      return 0;
   }

   mfem::Vector potential(fes.GetTrueVSize());
   potential = 0.0;
   mfem::Vector potential_rate(fes.GetTrueVSize());
   potential_rate = 0.0;

   // R100A-4 exposes q[0] as the physical pre-scaling source sample. The
   // solver-internal impulse mapping integrates the semidiscrete forcing
   // c^2*b*q across one native dt to create the causal velocity-potential
   // rate jump. The recorded denominator remains q[0], not this scaled vector.
   const double source_mass_relative_residual = SolveMassKick(
      M,
      source_functional,
      sound_speed_m_s * sound_speed_m_s * dt_s * source_amplitude_m3_s,
      potential_rate);

   HomogeneousPotentialWaveOperator oper(M, Kc2);
   mfem::NewmarkSolver ode_solver(0.25, 0.5);
   ode_solver.Init(oper);

   std::vector<TimeSample> samples;
   samples.reserve(sample_count);

   auto pressure_at_receiver = [&]()
   {
      return density_kg_m3 * (receiver_functional * potential_rate);
   };

   samples.push_back(TimeSample{
      pressure_at_receiver(),
      source_amplitude_m3_s,
   });

   double t = 0.0;
   double step_dt = dt_s;
   const auto solve_started = clock::now();
   for (size_t index = 1; index < sample_count; ++index)
   {
      step_dt = dt_s;
      ode_solver.Step(potential, potential_rate, t, step_dt);
      const double expected_t = static_cast<double>(index) * dt_s;
      if (std::abs(t - expected_t) > std::max(1.0e-12, expected_t * 1.0e-12))
      {
         throw std::runtime_error("MFEM time integrator changed the frozen native time grid");
      }
      samples.push_back(TimeSample{pressure_at_receiver(), 0.0});
   }
   const auto solve_finished = clock::now();
   const double solve_s =
      std::chrono::duration<double>(solve_finished - solve_started).count();

   WriteJson(
      output,
      order,
      uniform_refinements,
      mesh.GetNE(),
      fes.GetTrueVSize(),
      density_kg_m3,
      sound_speed_m_s,
      source_x,
      source_y,
      source_z,
      receiver_x,
      receiver_y,
      receiver_z,
      source_amplitude_m3_s,
      observation_time_s,
      sample_rate_hz,
      dt_s,
      assemble_s,
      solve_s,
      source_mass_relative_residual,
      oper.max_relative_residual(),
      oper.max_iterations(),
      samples);

   return 0;
}

} // namespace

int main(int argc, char *argv[])
{
   try
   {
      return ProbeMain(argc, argv);
   }
   catch (const std::exception &exc)
   {
      std::cerr << "R100B_MFEM_FINITE_RECORD_FATAL: " << exc.what() << std::endl;
      return 2;
   }
   catch (...)
   {
      std::cerr << "R100B_MFEM_FINITE_RECORD_FATAL: unknown exception" << std::endl;
      return 3;
   }
}
